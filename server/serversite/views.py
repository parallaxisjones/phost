import traceback

from django.http import (
    HttpResponse,
    JsonResponse,
    HttpResponseBadRequest,
    HttpResponseServerError,
    HttpResponseNotFound,
    HttpResponseForbidden,
)
from django.contrib.auth import authenticate, login
from django.db import transaction
from django.db.utils import IntegrityError
from django.utils.datastructures import MultiValueDictKeyError
from django.http.request import HttpRequest
from django.views.decorators.http import require_GET, require_POST
from django.views.generic import TemplateView

from .models import StaticDeployment, DeploymentVersion
from .forms import StaticDeploymentForm
from .upload import (
    handle_uploaded_static_archive,
    update_symlink,
    delete_hosted_deployment,
    delete_hosted_version,
)
from .serialize import serialize
from .validation import (
    BadInputException,
    validate_deployment_name,
    get_validated_form,
    NotFound,
    NotAuthenticated,
    InvalidCredentials,
    validate_subdomain,
)


def with_caught_exceptions(func):
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except BadInputException as e:
            return HttpResponseBadRequest(str(e))
        except NotFound:
            return HttpResponseNotFound()
        except NotAuthenticated:
            return HttpResponseForbidden("You must be logged into access this view")
        except InvalidCredentials:
            return HttpResponseForbidden("Invalid username or password provided")
        except Exception as e:
            print("Uncaught error: {}".format(str(e)))
            traceback.print_exc()

            return HttpResponseServerError(
                "An unhandled error occured while processing the request"
            )

    return wrapper


def with_default_success(func):
    """ Decorator that returns a JSON success message if no errors occur during the request. """

    def wrapper(*args, **kwargs):
        func(*args, **kwargs)
        return JsonResponse({"success": True, "error": False})

    return wrapper


def with_login_required(func):
    """ Decorator that verifies that a user is logged in and returns a Forbidden status code if
    the requester is not. """

    def wrapper(router: TemplateView, req: HttpRequest, *args, **kwargs):
        if not req.user.is_authenticated:
            raise NotAuthenticated()

        return func(router, req, *args, **kwargs)

    return wrapper


@require_GET
def index(_req: HttpRequest):
    return HttpResponse("Site is up and running!  Try `GET /deployments`.")


@require_POST
@with_caught_exceptions
@with_default_success
def login_user(req: HttpRequest):
    username = None
    password = None
    try:
        username = req.POST["username"]
        password = req.POST["password"]
    except MultiValueDictKeyError:
        raise BadInputException("You must supply both a username and password")

    user = authenticate(req, username=username, password=password)

    if user is not None:
        login(req, user)
    else:
        raise InvalidCredentials("Invalid username or password")


def get_or_none(Model, do_raise=True, **kwargs):
    """ Lookups a model given some query parameters.  If a match is found, it is returned.
    Otherwise, either `None` is returned or a `NotFound` exception is raised depending on
    the value of `do_raise`. """

    try:
        return Model.objects.get(**kwargs)
    except Model.DoesNotExist:
        if do_raise:
            raise NotFound()
        else:
            return None


class Deployments(TemplateView):
    @with_caught_exceptions
    @with_login_required
    def get(self, request: HttpRequest):
        all_deployments = StaticDeployment.objects.prefetch_related("deploymentversion_set").all()
        deployments_data = serialize(all_deployments, json=False)
        deployments_data_with_versions = [
            {
                **datum,
                "versions": serialize(deployment_models.deploymentversion_set.all(), json=False),
            }
            for (datum, deployment_models) in zip(deployments_data, all_deployments)
        ]

        return JsonResponse(deployments_data_with_versions, safe=False)

    @with_caught_exceptions
    @with_login_required
    def post(self, request: HttpRequest):
        form = get_validated_form(StaticDeploymentForm, request)

        deployment_name = form.cleaned_data["name"]
        subdomain = form.cleaned_data["subdomain"]
        version = form.cleaned_data["version"]
        validate_deployment_name(deployment_name)
        validate_subdomain(subdomain)

        deployment_descriptor = None
        try:
            with transaction.atomic():
                # Create the new deployment descriptor
                deployment_descriptor = StaticDeployment(name=deployment_name, subdomain=subdomain)
                deployment_descriptor.save()

                # Create the new version and set it as active
                version_model = DeploymentVersion(
                    version=version, deployment=deployment_descriptor, active=True
                )
                version_model.save()

                handle_uploaded_static_archive(request.FILES["file"], subdomain, version)
        except IntegrityError as e:
            if "Duplicate entry" in str(e):
                raise BadInputException("`name` and `subdomain` must be unique!")
            else:
                raise e

        return JsonResponse(
            {
                "name": deployment_name,
                "subdomain": subdomain,
                "version": version,
                "url": deployment_descriptor.get_url(),
            }
        )


def get_query_dict(query_string: str, req: HttpRequest) -> dict:
    lookup_field = req.GET.get("lookupField", "id")
    if lookup_field not in ["id", "subdomain", "name"]:
        raise BadInputException("The supplied `lookupField` was invalid")

    return {lookup_field: query_string}


class Deployment(TemplateView):
    @with_caught_exceptions
    def get(self, req: HttpRequest, deployment_id=None):
        query_dict = get_query_dict(deployment_id, req)
        deployment = get_or_none(StaticDeployment, **query_dict)
        versions = DeploymentVersion.objects.filter(deployment=deployment)
        active_version = next(v for v in versions if v.active)

        deployment_data = serialize(deployment, json=False)
        versions_data = serialize(versions, json=False)
        versions_list = list(map(lambda version_datum: version_datum["version"], versions_data))

        deployment_data = {
            **deployment_data,
            "versions": versions_list,
            "active_version": serialize(active_version, json=False)["version"],
        }

        return JsonResponse(deployment_data, safe=False)

    @with_caught_exceptions
    @with_login_required
    @with_default_success
    def delete(self, req: HttpRequest, deployment_id=None):
        with transaction.atomic():
            query_dict = get_query_dict(deployment_id, req)
            deployment = get_or_none(StaticDeployment, **query_dict)
            deployment_data = serialize(deployment, json=False)
            # This will also recursively delete all attached versions
            deployment.delete()

            delete_hosted_deployment(deployment_data["name"])


class DeploymentVersionView(TemplateView):
    @with_caught_exceptions
    def get(
        self, req: HttpRequest, *args, deployment_id=None, version=None
    ):  # pylint: disable=W0221
        query_dict = get_query_dict(deployment_id, req)
        deployment = get_or_none(StaticDeployment, **query_dict)
        version_model = get_or_none(DeploymentVersion, deployment=deployment, version=version)
        return serialize(version_model)

    @with_caught_exceptions
    @with_login_required
    def post(self, req: HttpRequest, *args, deployment_id=None, version=None):
        query_dict = get_query_dict(deployment_id, req)
        deployment = get_or_none(StaticDeployment, **query_dict)

        # Assert that the new version is unique among other versions for the same deployment
        if DeploymentVersion.objects.filter(deployment=deployment, version=version):
            raise BadInputException("The new version name must be unique.")

        version_model = None
        with transaction.atomic():
            # Set any old active deployment as inactive
            old_version = DeploymentVersion.objects.get(deployment=deployment, active=True)
            if old_version:
                old_version.active = False
                old_version.save()

            # Create the new version and set it active
            version_model = DeploymentVersion(version=version, deployment=deployment, active=True)
            version_model.save()

            deployment_data = serialize(deployment, json=False)

            # Extract the supplied archive into the hosting directory
            handle_uploaded_static_archive(
                req.FILES["file"], deployment_data["subdomain"], version, init=False
            )
            # Update the `latest` version to point to this new version
            update_symlink(deployment_data["name"], version)

        return serialize(version_model)

    @with_caught_exceptions
    @with_login_required
    @with_default_success
    def delete(self, req: HttpRequest, deployment_id=None, version=None):
        with transaction.atomic():
            query_dict = get_query_dict(deployment_id, req)
            deployment = get_or_none(StaticDeployment, **query_dict)
            # Delete the entry for the deployment version from the database
            DeploymentVersion.objects.filter(deployment=deployment, version=version).delete()
            # If no deployment versions remain for the owning deployment, delete the deployment
            delete_deployment = False
            if not DeploymentVersion.objects.filter(deployment=deployment):
                delete_deployment = True
                deployment.delete()

            if delete_deployment:
                delete_hosted_deployment(deployment)
            else:
                deployment_data = serialize(deployment, json=False)
                delete_hosted_version(deployment_data["name"], version)
