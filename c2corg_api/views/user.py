import datetime
import logging

import colander
import requests
from c2corg_api.emails.email_service import get_email_service
from c2corg_api.models import DBSession
from c2corg_api.models.document import DocumentLocale
from c2corg_api.models.user import (
        User, schema_user, schema_create_user, Purpose)
from c2corg_api.models.user_profile import UserProfile
from c2corg_api.search.notify_sync import notify_es_syncer
from c2corg_api.security.discourse_client import get_discourse_client
from c2corg_api.security.roles import (
    try_login, log_validated_user_i_know_what_i_do,
    remove_token, extract_token, renew_token)
from c2corg_api.views import (
        cors_policy, json_view, restricted_view, restricted_json_view,
        to_json_dict)
from c2corg_api.views.document import DocumentRest
from c2corg_api.views.validation import validate_required_json_string
from cornice.resource import resource
from cornice.validators import colander_body_validator
from functools import partial
from pyramid.httpexceptions import HTTPInternalServerError
from pyramid.settings import asbool
from sqlalchemy.sql.expression import and_

log = logging.getLogger(__name__)

ENCODING = 'UTF-8'
VALIDATION_EXPIRE_DAYS = 3
MINIMUM_PASSWORD_LENGTH = 3


def validate_json_password(request, **kwargs):
    """Checks if the password was given and encodes it.
       This is done here as the password is not an SQLAlchemy field.
       In addition, we can ensure the password is not leaked in the
       validation error messages.
    """

    if 'password' not in request.json:
        request.errors.add('body', 'password', 'Required')
        return

    password = request.json['password']
    if len(password) < MINIMUM_PASSWORD_LENGTH:
        request.errors.add('body', 'password', 'Password too short')
        return

    try:
        # We receive a unicode string. The hashing function used
        # later on requires plain string otherwise it raises
        # the "Unicode-objects must be encoded before hashing" error.
        request.validated['password'] = password.encode(ENCODING)
    except:
        request.errors.add('body', 'password', 'Invalid')


def is_unused_user_attribute(attrname, value):
    attr = getattr(User, attrname)
    return DBSession.query(User).filter(attr == value).count() == 0


def validate_unique_attribute(attrname, request, **kwargs):
    """Checks if the given attribute is unique.
    """

    if attrname in request.json:
        value = request.json[attrname]
        if is_unused_user_attribute(attrname, value):
            request.validated[attrname] = value
        else:
            request.errors.add('body', attrname, 'already used ' + attrname)


def validate_captcha(request, **kwargs):
    """Validate the recaptcha sent by UI.
    """

    settings = request.registry.settings
    if asbool(settings['skip.captcha.validation']):
        log.warning('Skipping captcha validation')
        return

    if 'captcha' not in request.json:
        request.errors.add('body', 'captcha', 'Missing captcha')
        return

    timeout = int(settings['url.timeout'])
    secret = settings['recaptcha.secret.key']
    captcha = request.json['captcha']

    url = 'https://www.google.com/recaptcha/api/siteverify'
    try:
        r = requests.post(url, timeout=timeout, data={
            'secret': secret,
            'response': captcha
            }
        )

        response = r.json()
        if not response['success']:
            request.errors.add('body', 'captcha', 'Error, please retry')
            return

    except:
        log.exception('Request error while checking captcha')
        # We want a notification and not a 500 to let the user immediately
        # resend a response.
        request.errors.add('body', 'captcha', 'Internal error, please retry')
        return


@resource(path='/users/register', cors_policy=cors_policy)
class UserRegistrationRest(object):
    def __init__(self, request):
        self.request = request

    @json_view(
        schema=schema_create_user,
        validators=[
            colander_body_validator,
            validate_json_password,
            partial(validate_unique_attribute, "email"),
            partial(validate_unique_attribute, "username"),
            partial(validate_unique_attribute, "forum_username"),
            validate_captcha])
    def post(self):
        user = schema_create_user.objectify(self.request.validated)
        user.password = self.request.validated['password']
        user.update_validation_nonce(
                Purpose.registration,
                VALIDATION_EXPIRE_DAYS)

        # directly create the user profile, the document id of the profile
        # is the user id
        lang = user.lang
        user.profile = UserProfile(
            categories=['amateur'],
            locales=[DocumentLocale(lang=lang, title='')]
        )

        DBSession.add(user)
        try:
            DBSession.flush()
        except:
            log.warning('Error persisting user', exc_info=True)
            raise HTTPInternalServerError('Error persisting user')

        # also create a version for the profile
        DocumentRest.create_new_version(user.profile, user.id)

        # The user needs validation
        email_service = get_email_service(self.request)
        nonce = user.validation_nonce
        settings = self.request.registry.settings
        link = settings['mail.validate_register_url_template'].format(
            '#', nonce)
        email_service.send_registration_confirmation(user, link)

        return to_json_dict(user, schema_user)


def validate_user_from_nonce(purpose, request, **kwargs):
    nonce = request.matchdict['nonce']
    if nonce is None:
        request.errors.add('querystring', 'nonce', 'missing nonce')
    else:
        now = datetime.datetime.utcnow()
        user = DBSession.query(User).filter(
                and_(
                    User.validation_nonce == nonce,
                    User.validation_nonce_expire > now)).first()
        if user is None:
            request.errors.add('querystring', 'nonce', 'invalid nonce')
        elif not user.validate_nonce_purpose(purpose):
            request.errors.add('querystring', 'nonce', 'unexpected purpose')
        else:
            request.validated['user'] = user


@resource(
        path='/users/validate_new_password/{nonce}',
        cors_policy=cors_policy)
class UserValidateNewPasswordRest(object):
    def __init__(self, request):
        self.request = request

    @json_view(validators=[
        partial(validate_user_from_nonce, Purpose.new_password),
        validate_json_password])
    def post(self):
        request = self.request
        user = request.validated['user']
        user.password = request.validated['password']

        # The user was validated by the nonce so we can log in
        token = log_validated_user_i_know_what_i_do(user, request)

        if token:
            settings = request.registry.settings
            response = token_to_response(user, token, request)
            try:
                client = get_discourse_client(settings)
                r = client.redirect_without_nonce(user)
                response['redirect_internal'] = r
            except:
                # Since only the password is changed, any error with discourse
                # must not prevent login and validation.
                log.error(
                    'Error logging into discourse for %d', user.id,
                    exc_info=True)

            user.clear_validation_nonce()
            try:
                DBSession.flush()
            except:
                log.warning('Error persisting user', exc_info=True)
                raise HTTPInternalServerError('Error persisting user')

            return response
        else:
            request.errors.status = 403
            request.errors.add('body', 'user', 'Login failed')
            return None


def validate_required_user_from_email(request, **kwargs):
    validate_required_json_string("email", request)
    if len(request.errors) != 0:
        return
    email = request.validated['email']
    user = DBSession.query(User).filter(User.email == email).first()
    if user:
        request.validated['user'] = user
    else:
        request.errors.add('body', 'email', 'No user with this email')


@resource(path='/users/request_password_change', cors_policy=cors_policy)
class UserRequestChangePasswordRest(object):
    def __init__(self, request):
        self.request = request

    @json_view(validators=[validate_required_user_from_email])
    def post(self):
        request = self.request
        user = request.validated['user']
        user.update_validation_nonce(
                Purpose.new_password,
                VALIDATION_EXPIRE_DAYS)

        try:
            DBSession.flush()
        except:
            log.warning('Error persisting user', exc_info=True)
            raise HTTPInternalServerError('Error persisting user')

        email_service = get_email_service(request)
        nonce = user.validation_nonce
        settings = request.registry.settings
        link = settings['mail.request_password_change_url_template'].format(
            '#', nonce)
        email_service.send_request_change_password(user, link)

        return {}


@resource(
        path='/users/validate_register_email/{nonce}',
        cors_policy=cors_policy)
class UserNonceValidationRest(object):
    def __init__(self, request):
        self.request = request

    @json_view(validators=[partial(
        validate_user_from_nonce, Purpose.registration)])
    def post(self):
        request = self.request
        user = request.validated['user']
        user.clear_validation_nonce()
        user.email_validated = True

        # the user profile can be indexed once the account is confirmed
        notify_es_syncer(self.request.registry.queue_config)

        # Synchronizing to Discourse is unnecessary as it will be done
        # during the redirect_without_nonce call below.

        # The user was validated by the nonce so we can log in
        token = log_validated_user_i_know_what_i_do(user, request)

        if token:
            response = token_to_response(user, token, request)
            settings = request.registry.settings
            try:
                client = get_discourse_client(settings)
                r = client.redirect_without_nonce(user)
                response['redirect_internal'] = r
            except:
                # Any error with discourse must prevent login and validation
                log.error(
                    'Error logging into discourse for %d', user.id,
                    exc_info=True)
                raise HTTPInternalServerError('Error with Discourse')

            try:
                DBSession.flush()
            except:
                log.warning('Error persisting user', exc_info=True)
                raise HTTPInternalServerError('Error persisting user')

            return response
        else:
            request.errors.status = 403
            request.errors.add('body', 'user', 'Login failed')
            return None


@resource(
        path='/users/validate_change_email/{nonce}',
        cors_policy=cors_policy)
class UserChangeEmailNonceValidationRest(object):
    def __init__(self, request):
        self.request = request

    @json_view(validators=[partial(
        validate_user_from_nonce, Purpose.change_email)])
    def post(self):
        request = self.request
        user = request.validated['user']
        user.clear_validation_nonce()
        user.email = user.email_to_validate
        user.email_to_validate = None

        # Synchronize the new email (and other parameters)
        try:
            client = get_discourse_client(request.registry.settings)
            client.sync_sso(user)
        except:
            log.error('Error syncing email with discourse', exc_info=True)
            raise HTTPInternalServerError('Error with Discourse')

        try:
            DBSession.flush()
        except:
            log.warning('Error persisting user', exc_info=True)
            raise HTTPInternalServerError('Error persisting user')

        # no login since user is supposed to be already logged in


class LoginSchema(colander.MappingSchema):
    username = colander.SchemaNode(colander.String())
    password = colander.SchemaNode(colander.String())

login_schema = LoginSchema()


def token_to_response(user, token, request):
    assert token is not None
    expire_time = token.expire - datetime.datetime(1970, 1, 1)
    roles = ['moderator'] if user.moderator else []
    return {
        'token': token.value,
        'username': user.username,
        'name': user.name,
        'forum_username': user.forum_username,
        'expire': int(expire_time.total_seconds()),
        'roles': roles,
        'id': user.id,
        'lang': user.lang
    }


@resource(path='/users/login', cors_policy=cors_policy)
class UserLoginRest(object):
    def __init__(self, request):
        self.request = request

    @json_view(
        schema=login_schema,
        validators=[colander_body_validator, validate_json_password])
    def post(self):
        request = self.request
        username = request.validated['username']
        password = request.validated['password']
        user = DBSession.query(User). \
            filter(User.username == username).first()

        token = try_login(user, password, request) if user else None
        if token:
            response = token_to_response(user, token, request)
            if 'discourse' in request.json:
                settings = request.registry.settings
                client = get_discourse_client(settings)
                try:
                    if 'sso' in request.json and 'sig' in request.json:
                        sso = request.json['sso']
                        sig = request.json['sig']
                        redirect = client.redirect(user, sso, sig)
                        response['redirect'] = redirect
                    else:
                        r = client.redirect_without_nonce(user)
                        response['redirect_internal'] = r
                except:
                    # Any error with discourse should not prevent login
                    log.warning(
                        'Error logging into discourse for %d', user.id,
                        exc_info=True)
            return response
        else:
            request.errors.status = 403
            request.errors.add('body', 'user', 'Login failed')
            return None


@resource(path='/users/renew', cors_policy=cors_policy)
class UserRenewRest(object):
    def __init__(self, request):
        self.request = request

    @restricted_view(renderer='json')
    def post(self):
        request = self.request
        userid = request.authenticated_userid
        user = DBSession.query(User).filter(User.id == userid).first()
        token = renew_token(user, request)
        if token:
            return token_to_response(user, token, request)
        else:
            raise HTTPInternalServerError('Error renewing token')


@resource(path='/users/logout', cors_policy=cors_policy)
class UserLogoutRest(object):
    def __init__(self, request):
        self.request = request

    @restricted_json_view(renderer='json')
    def post(self):
        request = self.request
        userid = request.authenticated_userid
        result = {'user': userid}
        remove_token(extract_token(request))
        if 'discourse' in request.json:
            try:
                settings = request.registry.settings
                client = get_discourse_client(settings)
                result['logged_out_discourse_user'] = client.logout(userid)
            except:
                # Any error with discourse should not prevent logout
                log.warning(
                    'Error logging out of discourse for %d', userid,
                    exc_info=True)
        return result
