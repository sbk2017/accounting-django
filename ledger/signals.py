import logging
from django.contrib.auth.signals import user_logged_in,user_login_failed
from django.dispatch import receiver
log=logging.getLogger('ledger.auth')
@receiver(user_logged_in)
def logged_in(sender,request,user,**kwargs):
    log.info('Login succeeded username=%s remote=%s',user.get_username(),request.META.get('REMOTE_ADDR'))
@receiver(user_login_failed)
def failed(sender,credentials,request,**kwargs):
    log.warning('Login failed username=%s remote=%s',credentials.get('username',''),request.META.get('REMOTE_ADDR') if request else '')
