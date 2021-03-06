# -*- coding: utf-8 -*-
"""
Models for Student Identity Verification

This is where we put any models relating to establishing the real-life identity
of a student over a period of time. Right now, the only models are the abstract
`PhotoVerification`, and its one concrete implementation
`SoftwareSecurePhotoVerification`. The hope is to keep as much of the
photo verification process as generic as possible.
"""
from datetime import datetime, timedelta
from email.utils import formatdate
from hashlib import md5
import base64
import functools
import json
import logging
import uuid

from boto.s3.connection import S3Connection
from boto.s3.key import Key
import pytz
import requests

from django.conf import settings
from django.core.urlresolvers import reverse
from django.db import models
from django.contrib.auth.models import User
from model_utils.models import StatusModel
from model_utils import Choices

from verify_student.ssencrypt import (
    random_aes_key, encrypt_and_encode,
    generate_signed_message, rsa_encrypt
)

log = logging.getLogger(__name__)


class VerificationException(Exception):
    pass


def status_before_must_be(*valid_start_statuses):
    """
    Helper decorator with arguments to make sure that an object with a `status`
    attribute is in one of a list of acceptable status states before a method
    is called. You could use it in a class definition like:

        @status_before_must_be("submitted", "approved", "denied")
        def refund_user(self, user_id):
            # Do logic here...

    If the object has a status that is not listed when the `refund_user` method
    is invoked, it will throw a `VerificationException`. This is just to avoid
    distracting boilerplate when looking at a Model that needs to go through a
    workflow process.
    """
    def decorator_func(func):
        """
        Decorator function that gets returned
        """
        @functools.wraps(func)
        def with_status_check(obj, *args, **kwargs):
            if obj.status not in valid_start_statuses:
                exception_msg = (
                    u"Error calling {} {}: status is '{}', must be one of: {}"
                ).format(func, obj, obj.status, valid_start_statuses)
                raise VerificationException(exception_msg)
            return func(obj, *args, **kwargs)

        return with_status_check

    return decorator_func


class PhotoVerification(StatusModel):
    """
    Each PhotoVerification represents a Student's attempt to establish
    their identity by uploading a photo of themselves and a picture ID. An
    attempt actually has a number of fields that need to be filled out at
    different steps of the approval process. While it's useful as a Django Model
    for the querying facilities, **you should only edit a `PhotoVerification`
    object through the methods provided**. Initialize them with a user:

    attempt = PhotoVerification(user=user)

    We track this attempt through various states:

    `created`
        Initial creation and state we're in after uploading the images.
    `ready`
        The user has uploaded their images and checked that they can read the
        images. There's a separate state here because it may be the case that we
        don't actually submit this attempt for review until payment is made.
    `submitted`
        Submitted for review. The review may be done by a staff member or an
        external service. The user cannot make changes once in this state.
    `must_retry`
        We submitted this, but there was an error on submission (i.e. we did not
        get a 200 when we POSTed to Software Secure)
    `approved`
        An admin or an external service has confirmed that the user's photo and
        photo ID match up, and that the photo ID's name matches the user's.
    `denied`
        The request has been denied. See `error_msg` for details on why. An
        admin might later override this and change to `approved`, but the
        student cannot re-open this attempt -- they have to create another
        attempt and submit it instead.

    Because this Model inherits from StatusModel, we can also do things like::

        attempt.status == PhotoVerification.STATUS.created
        attempt.status == "created"
        pending_requests = PhotoVerification.submitted.all()
    """
    # We can make this configurable later...
    DAYS_GOOD_FOR = settings.VERIFY_STUDENT["DAYS_GOOD_FOR"]

    ######################## Fields Set During Creation ########################
    # See class docstring for description of status states
    STATUS = Choices('created', 'ready', 'submitted', 'must_retry', 'approved', 'denied')
    user = models.ForeignKey(User, db_index=True)

    # They can change their name later on, so we want to copy the value here so
    # we always preserve what it was at the time they requested. We only copy
    # this value during the mark_ready() step. Prior to that, you should be
    # displaying the user's name from their user.profile.name.
    name = models.CharField(blank=True, max_length=255)

    # Where we place the uploaded image files (e.g. S3 URLs)
    face_image_url = models.URLField(blank=True, max_length=255)
    photo_id_image_url = models.URLField(blank=True, max_length=255)

    # Randomly generated UUID so that external services can post back the
    # results of checking a user's photo submission without use exposing actual
    # user IDs or something too easily guessable.
    receipt_id = models.CharField(
        db_index=True,
        default=uuid.uuid4,
        max_length=255,
    )

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True, db_index=True)

    ######################## Fields Set When Submitting ########################
    submitted_at = models.DateTimeField(null=True, db_index=True)

    #################### Fields Set During Approval/Denial #####################
    # If the review was done by an internal staff member, mark who it was.
    reviewing_user = models.ForeignKey(
        User,
        db_index=True,
        default=None,
        null=True,
        related_name="photo_verifications_reviewed"
    )

    # Mark the name of the service used to evaluate this attempt (e.g
    # Software Secure).
    reviewing_service = models.CharField(blank=True, max_length=255)

    # If status is "denied", this should contain text explaining why.
    error_msg = models.TextField(blank=True)

    # Non-required field. External services can add any arbitrary codes as time
    # goes on. We don't try to define an exhuastive list -- this is just
    # capturing it so that we can later query for the common problems.
    error_code = models.CharField(blank=True, max_length=50)

    class Meta:
        abstract = True
        ordering = ['-created_at']

    ##### Methods listed in the order you'd typically call them
    @classmethod
    def user_is_verified(cls, user, earliest_allowed_date=None):
        """
        Returns whether or not a user has satisfactorily proved their
        identity. Depending on the policy, this can expire after some period of
        time, so a user might have to renew periodically.
        """
        earliest_allowed_date = (
            earliest_allowed_date or
            datetime.now(pytz.UTC) - timedelta(days=cls.DAYS_GOOD_FOR)
        )
        return cls.objects.filter(
            user=user,
            status="approved",
            created_at__gte=earliest_allowed_date
        ).exists()

    @classmethod
    def user_has_valid_or_pending(cls, user, earliest_allowed_date=None):
        """
        TODO: eliminate duplication with user_is_verified
        """
        valid_statuses = ['must_retry', 'submitted', 'approved']
        earliest_allowed_date = (
            earliest_allowed_date or
            datetime.now(pytz.UTC) - timedelta(days=cls.DAYS_GOOD_FOR)
        )
        return cls.objects.filter(
            user=user,
            status__in=valid_statuses,
            created_at__gte=earliest_allowed_date
        ).exists()

    @classmethod
    def active_for_user(cls, user):
        """
        Return all PhotoVerifications that are still active (i.e. not
        approved or denied).

        Should there only be one active at any given time for a user? Enforced
        at the DB level?
        """
        # This should only be one at the most, but just in case we create more
        # by mistake, we'll grab the most recently created one.
        active_attempts = cls.objects.filter(user=user, status='ready').order_by('-created_at')
        if active_attempts:
            return active_attempts[0]
        else:
            return None

    @status_before_must_be("created")
    def upload_face_image(self, img):
        raise NotImplementedError

    @status_before_must_be("created")
    def upload_photo_id_image(self, img):
        raise NotImplementedError

    @status_before_must_be("created")
    def mark_ready(self):
        """
        Mark that the user data in this attempt is correct. In order to
        succeed, the user must have uploaded the necessary images
        (`face_image_url`, `photo_id_image_url`). This method will also copy
        their name from their user profile. Prior to marking it ready, we read
        this value directly from their profile, since they're free to change it.
        This often happens because people put in less formal versions of their
        name on signup, but realize they want something different to go on a
        formal document.

        Valid attempt statuses when calling this method:
            `created`

        Status after method completes: `ready`

        Other fields that will be set by this method:
            `name`

        State Transitions:

        `created` → `ready`
            This is what happens when the user confirms to us that the pictures
            they uploaded are good. Note that we don't actually do a submission
            anywhere yet.
        """
        # At any point prior to this, they can change their names via their
        # student dashboard. But at this point, we lock the value into the
        # attempt.
        self.name = self.user.profile.name
        self.status = "ready"
        self.save()

    @status_before_must_be("must_retry", "ready", "submitted")
    def submit(self):
        raise NotImplementedError

    @status_before_must_be("must_retry", "submitted", "approved", "denied")
    def approve(self, user_id=None, service=""):
        """
        Approve this attempt. `user_id`

        Valid attempt statuses when calling this method:
            `submitted`, `approved`, `denied`

        Status after method completes: `approved`

        Other fields that will be set by this method:
            `reviewed_by_user_id`, `reviewed_by_service`, `error_msg`

        State Transitions:

        `submitted` → `approved`
            This is the usual flow, whether initiated by a staff user or an
            external validation service.
        `approved` → `approved`
            No-op. First one to approve it wins.
        `denied` → `approved`
            This might happen if a staff member wants to override a decision
            made by an external service or another staff member (say, in
            response to a support request). In this case, the previous values
            of `reviewed_by_user_id` and `reviewed_by_service` will be changed
            to whoever is doing the approving, and `error_msg` will be reset.
            The only record that this record was ever denied would be in our
            logs. This should be a relatively rare occurence.
        """
        # If someone approves an outdated version of this, the first one wins
        if self.status == "approved":
            return

        self.error_msg = ""  # reset, in case this attempt was denied before
        self.error_code = ""  # reset, in case this attempt was denied before
        self.reviewing_user = user_id
        self.reviewing_service = service
        self.status = "approved"
        self.save()

    @status_before_must_be("must_retry", "submitted", "approved", "denied")
    def deny(self,
             error_msg,
             error_code="",
             reviewing_user=None,
             reviewing_service=""):
        """
        Deny this attempt.

        Valid attempt statuses when calling this method:
            `submitted`, `approved`, `denied`

        Status after method completes: `denied`

        Other fields that will be set by this method:
            `reviewed_by_user_id`, `reviewed_by_service`, `error_msg`,
            `error_code`

        State Transitions:

        `submitted` → `denied`
            This is the usual flow, whether initiated by a staff user or an
            external validation service.
        `approved` → `denied`
            This might happen if a staff member wants to override a decision
            made by an external service or another staff member, or just correct
            a mistake made during the approval process. In this case, the
            previous values of `reviewed_by_user_id` and `reviewed_by_service`
            will be changed to whoever is doing the denying. The only record
            that this record was ever approved would be in our logs. This should
            be a relatively rare occurence.
        `denied` → `denied`
            Update the error message and reviewing_user/reviewing_service. Just
            lets you amend the error message in case there were additional
            details to be made.
        """
        self.error_msg = error_msg
        self.error_code = error_code
        self.reviewing_user = reviewing_user
        self.reviewing_service = reviewing_service
        self.status = "denied"
        self.save()

    @status_before_must_be("must_retry", "submitted", "approved", "denied")
    def system_error(self,
                     error_msg,
                     error_code="",
                     reviewing_user=None,
                     reviewing_service=""):
        """
        Mark that this attempt could not be completed because of a system error.
        Status should be moved to `must_retry`.
        """
        if self.status in ["approved", "denied"]:
            return  # If we were already approved or denied, just leave it.

        self.error_msg = error_msg
        self.error_code = error_code
        self.reviewing_user = reviewing_user
        self.reviewing_service = reviewing_service
        self.status = "must_retry"
        self.save()


class SoftwareSecurePhotoVerification(PhotoVerification):
    """
    Model to verify identity using a service provided by Software Secure. Much
    of the logic is inherited from `PhotoVerification`, but this class
    encrypts the photos.

    Software Secure (http://www.softwaresecure.com/) is a remote proctoring
    service that also does identity verification. A student uses their webcam
    to upload two images: one of their face, one of a photo ID. Due to the
    sensitive nature of the data, the following security precautions are taken:

    1. The snapshot of their face is encrypted using AES-256 in CBC mode. All
       face photos are encypted with the same key, and this key is known to
       both Software Secure and edx-platform.

    2. The snapshot of a user's photo ID is also encrypted using AES-256, but
       the key is randomly generated using pycrypto's Random. Every verification
       attempt has a new key. The AES key is then encrypted using a public key
       provided by Software Secure. We store only the RSA-encryped AES key.
       Since edx-platform does not have Software Secure's private RSA key, it
       means that we can no longer even read photo ID.

    3. The encrypted photos are base64 encoded and stored in an S3 bucket that
       edx-platform does not have read access to.
    """
    # This is a base64.urlsafe_encode(rsa_encrypt(photo_id_aes_key), ss_pub_key)
    # So first we generate a random AES-256 key to encrypt our photo ID with.
    # Then we RSA encrypt it with Software Secure's public key. Then we base64
    # encode that. The result is saved here. Actual expected length is 344.
    photo_id_key = models.TextField(max_length=1024)

    IMAGE_LINK_DURATION = 5 * 60 * 60 * 24  # 5 days in seconds

    @status_before_must_be("created")
    def upload_face_image(self, img_data):
        """
        Upload an image of the user's face to S3. `img_data` should be a raw
        bytestream of a PNG image. This method will take the data, encrypt it
        using our FACE_IMAGE_AES_KEY, encode it with base64 and save it to S3.

        Yes, encoding it to base64 adds compute and disk usage without much real
        benefit, but that's what the other end of this API is expecting to get.
        """
        # Skip this whole thing if we're running acceptance tests or if we're
        # developing and aren't interested in working on student identity
        # verification functionality. If you do want to work on it, you have to
        # explicitly enable these in your private settings.
        if settings.MITX_FEATURES.get('AUTOMATIC_VERIFY_STUDENT_IDENTITY_FOR_TESTING'):
            return

        aes_key_str = settings.VERIFY_STUDENT["SOFTWARE_SECURE"]["FACE_IMAGE_AES_KEY"]
        aes_key = aes_key_str.decode("hex")

        s3_key = self._generate_key("face")
        s3_key.set_contents_from_string(encrypt_and_encode(img_data, aes_key))

    @status_before_must_be("created")
    def upload_photo_id_image(self, img_data):
        """
        Upload an the user's photo ID image to S3. `img_data` should be a raw
        bytestream of a PNG image. This method will take the data, encrypt it
        using a randomly generated AES key, encode it with base64 and save it to
        S3. The random key is also encrypted using Software Secure's public RSA
        key and stored in our `photo_id_key` field.

        Yes, encoding it to base64 adds compute and disk usage without much real
        benefit, but that's what the other end of this API is expecting to get.
        """
        # Skip this whole thing if we're running acceptance tests or if we're
        # developing and aren't interested in working on student identity
        # verification functionality. If you do want to work on it, you have to
        # explicitly enable these in your private settings.
        if settings.MITX_FEATURES.get('AUTOMATIC_VERIFY_STUDENT_IDENTITY_FOR_TESTING'):
            return

        aes_key = random_aes_key()
        rsa_key_str = settings.VERIFY_STUDENT["SOFTWARE_SECURE"]["RSA_PUBLIC_KEY"]
        rsa_encrypted_aes_key = rsa_encrypt(aes_key, rsa_key_str)

        # Upload this to S3
        s3_key = self._generate_key("photo_id")
        s3_key.set_contents_from_string(encrypt_and_encode(img_data, aes_key))

        # Update our record fields
        self.photo_id_key = rsa_encrypted_aes_key.encode('base64')
        self.save()

    @status_before_must_be("must_retry", "ready", "submitted")
    def submit(self):
        """
        Submit our verification attempt to Software Secure for validation. This
        will set our status to "submitted" if the post is successful, and
        "must_retry" if the post fails.
        """
        try:
            response = self.send_request()
            if response.ok:
                self.submitted_at = datetime.now(pytz.UTC)
                self.status = "submitted"
                self.save()
            else:
                self.status = "must_retry"
                self.error_msg = response.text
                self.save()
        except Exception as error:
            log.exception(error)

    def image_url(self, name):
        """
        We dynamically generate this, since we want it the expiration clock to
        start when the message is created, not when the record is created.
        """
        s3_key = self._generate_key(name)
        return s3_key.generate_url(self.IMAGE_LINK_DURATION)

    def _generate_key(self, prefix):
        """
        Example: face/4dd1add9-6719-42f7-bea0-115c008c4fca
        """
        conn = S3Connection(
            settings.VERIFY_STUDENT["SOFTWARE_SECURE"]["AWS_ACCESS_KEY"],
            settings.VERIFY_STUDENT["SOFTWARE_SECURE"]["AWS_SECRET_KEY"]
        )
        bucket = conn.get_bucket(settings.VERIFY_STUDENT["SOFTWARE_SECURE"]["S3_BUCKET"])

        key = Key(bucket)
        key.key = "{}/{}".format(prefix, self.receipt_id)

        return key

    def _encrypted_user_photo_key_str(self):
        """
        Software Secure needs to have both UserPhoto and PhotoID decrypted in
        the same manner. So even though this is going to be the same for every
        request, we're also using RSA encryption to encrypt the AES key for
        faces.
        """
        face_aes_key_str = settings.VERIFY_STUDENT["SOFTWARE_SECURE"]["FACE_IMAGE_AES_KEY"]
        face_aes_key = face_aes_key_str.decode("hex")
        rsa_key_str = settings.VERIFY_STUDENT["SOFTWARE_SECURE"]["RSA_PUBLIC_KEY"]
        rsa_encrypted_face_aes_key = rsa_encrypt(face_aes_key, rsa_key_str)

        return rsa_encrypted_face_aes_key.encode("base64")

    def create_request(self):
        """return headers, body_dict"""
        access_key = settings.VERIFY_STUDENT["SOFTWARE_SECURE"]["API_ACCESS_KEY"]
        secret_key = settings.VERIFY_STUDENT["SOFTWARE_SECURE"]["API_SECRET_KEY"]

        scheme = "https" if settings.HTTPS == "on" else "http"
        callback_url = "{}://{}{}".format(
            scheme, settings.SITE_NAME, reverse('verify_student_results_callback')
        )

        body = {
            "EdX-ID": str(self.receipt_id),
            "ExpectedName": self.name,
            "PhotoID": self.image_url("photo_id"),
            "PhotoIDKey": self.photo_id_key,
            "SendResponseTo": callback_url,
            "UserPhoto": self.image_url("face"),
            "UserPhotoKey": self._encrypted_user_photo_key_str(),
        }
        headers = {
            "Content-Type": "application/json",
            "Date": formatdate(timeval=None, localtime=False, usegmt=True)
        }
        _message, _sig, authorization = generate_signed_message(
            "POST", headers, body, access_key, secret_key
        )
        headers['Authorization'] = authorization

        return headers, body

    def request_message_txt(self):
        """This is the body of the request we send across."""
        headers, body = self.create_request()

        header_txt = "\n".join(
            "{}: {}".format(h, v) for h, v in sorted(headers.items())
        )
        body_txt = json.dumps(body, indent=2, sort_keys=True, ensure_ascii=False).encode('utf-8')

        return header_txt + "\n\n" + body_txt

    def send_request(self):
        """
        Assembles a submission to Software Secure and sends it via HTTPS.

        Returns a request.Response() object with the reply we get from SS.
        """
        # If AUTOMATIC_VERIFY_STUDENT_IDENTITY_FOR_TESTING is True, we want to
        # skip posting anything to Software Secure. We actually don't even
        # create the message because that would require encryption and message
        # signing that rely on settings.VERIFY_STUDENT values that aren't set
        # in dev. So we just pretend like we successfully posted
        if settings.MITX_FEATURES.get('AUTOMATIC_VERIFY_STUDENT_IDENTITY_FOR_TESTING'):
            fake_response = requests.Response()
            fake_response.status_code = 200
            return fake_response

        headers, body = self.create_request()
        response = requests.post(
            settings.VERIFY_STUDENT["SOFTWARE_SECURE"]["API_URL"],
            headers=headers,
            data=json.dumps(body, indent=2, sort_keys=True, ensure_ascii=False).encode('utf-8'),
            verify=False
        )
        log.debug("Sent request to Software Secure for {}".format(self.receipt_id))
        log.debug("Headers:\n{}\n\n".format(headers))
        log.debug("Body:\n{}\n\n".format(body))
        log.debug("Return code: {}".format(response.status_code))
        log.debug("Return message:\n\n{}\n\n".format(response.text))

        return response
