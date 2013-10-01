"""
Unit tests for course import and export
"""
import os
import shutil
import tarfile
import tempfile
import copy
import json
import logging
from uuid import uuid4
from pymongo import MongoClient

from .utils import CourseTestCase
from django.contrib.auth.models import User
from django.core.urlresolvers import reverse
from django.test.utils import override_settings
from django.conf import settings

from xmodule.contentstore.django import _CONTENTSTORE

TEST_DATA_CONTENTSTORE = copy.deepcopy(settings.CONTENTSTORE)
TEST_DATA_CONTENTSTORE['OPTIONS']['db'] = 'test_xcontent_%s' % uuid4().hex

log = logging.getLogger(__name__)

@override_settings(CONTENTSTORE=TEST_DATA_CONTENTSTORE)
class ImportTestCase(CourseTestCase):
    """
    Unit tests for importing a course
    """

    def setUp(self):
        super(ImportTestCase, self).setUp()
        self.url = reverse("import_course", kwargs={
            'org': self.course.location.org,
            'course': self.course.location.course,
            'name': self.course.location.name,
        })
        self.content_dir = tempfile.mkdtemp()

        def touch(name):
            """ Equivalent to shell's 'touch'"""
            with file(name, 'a'):
                os.utime(name, None)

        # Create tar test files -----------------------------------------------
        # OK course:
        good_dir = tempfile.mkdtemp(dir=self.content_dir)
        os.makedirs(os.path.join(good_dir, "course"))
        with open(os.path.join(good_dir, "course.xml"), "w+") as f:
            f.write('<course url_name="2013_Spring" org="EDx" course="0.00x"/>')

        with open(os.path.join(good_dir, "course", "2013_Spring.xml"), "w+") as f:
            f.write('<course></course>')

        self.good_tar = os.path.join(self.content_dir, "good.tar.gz")
        with tarfile.open(self.good_tar, "w:gz") as gtar:
            gtar.add(good_dir)

        # Bad course (no 'course.xml' file):
        bad_dir = tempfile.mkdtemp(dir=self.content_dir)
        touch(os.path.join(bad_dir, "bad.xml"))
        self.bad_tar = os.path.join(self.content_dir, "bad.tar.gz")
        with tarfile.open(self.bad_tar, "w:gz") as btar:
            btar.add(bad_dir)

    def tearDown(self):
        shutil.rmtree(self.content_dir)
        MongoClient().drop_database(TEST_DATA_CONTENTSTORE['OPTIONS']['db'])
        _CONTENTSTORE.clear()

    def test_no_coursexml(self):
        """
        Check that the response for a tar.gz import without a course.xml is
        correct.
        """
        with open(self.bad_tar) as btar:
            resp = self.client.post(
                self.url,
                {
                    "name": self.bad_tar,
                    "course-data": [btar]
                })
        self.assertEquals(resp.status_code, 415)
        # Check that `import_status` returns the appropriate stage (i.e., the
        # stage at which import failed).
        status_url = reverse("import_status", kwargs={
            'org': self.course.location.org,
            'course': self.course.location.course,
            'name': os.path.split(self.bad_tar)[1],
        })
        resp_status = self.client.get(status_url)
        log.debug(str(self.client.session["import_status"]))
        self.assertEquals(json.loads(resp_status.content)["ImportStatus"], 2)


    def test_with_coursexml(self):
        """
        Check that the response for a tar.gz import with a course.xml is
        correct.
        """
        with open(self.good_tar) as gtar:
            resp = self.client.post(
                    self.url,
                    {
                        "name": self.good_tar,
                        "course-data": [gtar]
                    })
        self.assertEquals(resp.status_code, 200)
        # Check that `import_status` returns the appropriate stage (i.e.,
        # either 3, indicating all previous steps are completed, or 0,
        # indicating no upload in progress)
        status_url = reverse("import_status", kwargs={
            'org': self.course.location.org,
            'course': self.course.location.course,
            'name': os.path.split(self.good_tar)[1],
        })
        resp_status = self.client.get(status_url)
        import_status = json.loads(resp_status.content)["ImportStatus"]
        self.assertTrue(import_status == 3 or import_status == 0)
