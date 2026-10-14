import os
from pathlib import Path
BASE_DIR=Path(__file__).resolve().parent.parent
SECRET_KEY=os.getenv('DJANGO_SECRET_KEY','insecure-local-development-only')
DEBUG=os.getenv('DJANGO_DEBUG','0')=='1'
ALLOWED_HOSTS=[s.strip() for s in os.getenv('ALLOWED_HOSTS','localhost,127.0.0.1').split(',') if s.strip()]
CSRF_TRUSTED_ORIGINS=[s.strip() for s in os.getenv('CSRF_TRUSTED_ORIGINS','').split(',') if s.strip()]
INSTALLED_APPS=['django.contrib.admin','django.contrib.auth','django.contrib.contenttypes','django.contrib.sessions','django.contrib.messages','django.contrib.staticfiles','ledger.apps.LedgerConfig']
MIDDLEWARE=['django.middleware.security.SecurityMiddleware','django.contrib.sessions.middleware.SessionMiddleware','django.middleware.common.CommonMiddleware','django.middleware.csrf.CsrfViewMiddleware','django.contrib.auth.middleware.AuthenticationMiddleware','django.contrib.messages.middleware.MessageMiddleware','django.middleware.clickjacking.XFrameOptionsMiddleware']
ROOT_URLCONF='config.urls'
TEMPLATES=[{'BACKEND':'django.template.backends.django.DjangoTemplates','DIRS':[BASE_DIR/'templates'],'APP_DIRS':True,'OPTIONS':{'context_processors':['django.template.context_processors.debug','django.template.context_processors.request','django.contrib.auth.context_processors.auth','django.contrib.messages.context_processors.messages']}}]
WSGI_APPLICATION='config.wsgi.application'
DATA_DIR=Path(os.getenv('DATA_DIR',BASE_DIR/'data'));DATA_DIR.mkdir(parents=True,exist_ok=True)
DATABASES={'default':{'ENGINE':'django.db.backends.sqlite3','NAME':DATA_DIR/'accounting.sqlite3','OPTIONS':{'timeout':20}}}
AUTH_PASSWORD_VALIDATORS=[{'NAME':'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},{'NAME':'django.contrib.auth.password_validation.MinimumLengthValidator'},{'NAME':'django.contrib.auth.password_validation.CommonPasswordValidator'},{'NAME':'django.contrib.auth.password_validation.NumericPasswordValidator'}]
LANGUAGE_CODE='en-us';TIME_ZONE='Asia/Dubai';USE_TZ=True
STATIC_URL='static/';DEFAULT_AUTO_FIELD='django.db.models.BigAutoField'
LOGIN_URL='login';LOGIN_REDIRECT_URL='dashboard';LOGOUT_REDIRECT_URL='login'
SESSION_COOKIE_HTTPONLY=True;SESSION_COOKIE_SAMESITE='Lax'
# Keep the CSRF secret in the login session instead of a separate browser cookie.
CSRF_USE_SESSIONS=True
LOGGING={'version':1,'disable_existing_loggers':False,'formatters':{'verbose':{'format':'%(asctime)s %(levelname)s %(name)s %(message)s'}},'handlers':{'console':{'class':'logging.StreamHandler','formatter':'verbose'},'file':{'class':'logging.handlers.RotatingFileHandler','filename':str(DATA_DIR/'app.log'),'maxBytes':2000000,'backupCount':3,'formatter':'verbose'}},'root':{'handlers':['console','file'],'level':os.getenv('LOG_LEVEL','INFO')},'loggers':{'django.request':{'handlers':['console','file'],'level':'WARNING','propagate':False}}}

# Separate read-only credential; blank disables the reporting API.
REPORTING_API_TOKEN=os.getenv('REPORTING_API_TOKEN','')
