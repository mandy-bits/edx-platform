"""
Exposes Django utilities for consumption in the xmodule library
NOTE: This file should only be imported into 'django-safe' code, i.e. known that this code runs int the Django 
runtime environment with the djangoapps in common configured to load
"""

from crum import get_current_request as crum_get_current_request

def get_current_request():
	"""
	Use the tls middleware utility to get the current request on this thread
	"""
	return crum_get_current_request()

def get_current_request_hostname():
	"""
	This method will return the hostname that was used in the current Django request
	"""
	hostname = None
	request = get_current_request()
	if request:
		hostname = request.META.get('HTTP_HOST')

	return hostname
