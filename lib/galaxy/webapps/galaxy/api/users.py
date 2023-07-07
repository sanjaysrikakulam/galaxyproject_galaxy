"""
API operations on User objects.
"""
import copy
import json
import logging
import re
from typing import (
    Any,
    Dict,
    List,
    Optional,
    Union,
)

from fastapi import (
    Body,
    Path,
    Query,
    Response,
    status,
)
from markupsafe import escape
from pydantic import Required

from galaxy import (
    exceptions,
    util,
)
from galaxy.exceptions import ObjectInvalid
from galaxy.managers import users
from galaxy.managers.context import (
    ProvidesHistoryContext,
    ProvidesUserContext,
)
from galaxy.model import (
    UserAddress,
    UserQuotaUsage,
)
from galaxy.model.base import transaction
from galaxy.schema import APIKeyModel
from galaxy.schema.fields import DecodedDatabaseIdField
from galaxy.schema.schema import (
    AnonUserModel,
    AsyncTaskResultSummary,
    CreatedUserModel,
    CreateUserPayload,
    DetailedUserModel,
    FlexibleUserIdType,
    PurgeUserPayload,
    UserBeaconSetting,
    UserModel,
)
from galaxy.security.validate_user_input import (
    validate_email,
    validate_password,
    validate_publicname,
)
from galaxy.security.vault import UserVaultWrapper
from galaxy.tool_util.toolbox.filters import FilterFactory
from galaxy.util import (
    docstring_trim,
    listify,
)
from galaxy.web import expose_api
from galaxy.web.form_builder import AddressField
from galaxy.webapps.base.controller import (
    BaseUIController,
    UsesFormDefinitionsMixin,
    UsesTagsMixin,
)
from galaxy.webapps.galaxy.api import (
    BaseGalaxyAPIController,
    depends,
    DependsOnTrans,
    Router,
)
from galaxy.webapps.galaxy.services.users import UsersService

log = logging.getLogger(__name__)

router = Router(tags=["users"])

ThemePathParam: str = Path(default=Required, title="Theme", description="The theme of the GUI")
UserDeleted: bool = Query(default=None, title="Deleted User", description="Indicates if the user is deleted")
UsersDeleted: bool = Query(default=False)
FilterEmail: str = Query(default=None)
FilterName: str = Query(default=None)
FilterAny: str = Query(default=None)
UserIdPathParam: DecodedDatabaseIdField = Path(..., title="User ID", description="The ID of the user to get.")
APIKeyPathParam: str = Path(..., title="API Key", description="The API key of the user.")
FlexibleUserIdPathParam: FlexibleUserIdType = Path(
    ..., title="User ID", description="The ID of the user to get or 'current'."
)
QuotaSourceLabelPathParam: str = Path(
    ...,
    title="Quota Source Label",
    description="The label corresponding to the quota source to fetch usage information about.",
)

RecalculateDiskUsageSummary = "Triggers a recalculation of the current user disk usage."
RecalculateDiskUsageResponseDescriptions = {
    200: {
        "model": AsyncTaskResultSummary,
        "description": "The asynchronous task summary to track the task state.",
    },
    204: {
        "description": "The background task was submitted but there is no status tracking ID available.",
    },
}

CreateUserBody = Body(default=Required, title="Create user", description="The values to create a user.")
PurgeUserBody = Body(default=None, title="Purge user", description="Purge the user.")
UpdateUserBody = Body(default=Required, title="Update user", description="The user values to update.")
AnyUserModel = Union[DetailedUserModel, AnonUserModel]


@router.cbv
class FastAPIUsers:
    service: UsersService = depends(UsersService)
    user_serializer: users.UserSerializer = depends(users.UserSerializer)

    @router.put(
        "/api/users/current/recalculate_disk_usage",
        summary=RecalculateDiskUsageSummary,
        responses=RecalculateDiskUsageResponseDescriptions,
    )
    @router.put(
        "/api/users/recalculate_disk_usage",
        summary=RecalculateDiskUsageSummary,
        responses=RecalculateDiskUsageResponseDescriptions,
        deprecated=True,
    )
    def recalculate_disk_usage(
        self,
        trans: ProvidesUserContext = DependsOnTrans,
    ):
        """This route will be removed in a future version.

        Please use `/api/users/current/recalculate_disk_usage` instead.
        """
        result = self.service.recalculate_disk_usage(trans)
        return Response(status_code=status.HTTP_204_NO_CONTENT) if result is None else result

    @router.get("/api/users/deleted", name="Get deleted users", description="Display a collection of deleted users")
    def index_deleted(
        self,
        trans: ProvidesUserContext = DependsOnTrans,
        f_email: str = FilterEmail,
        f_name: str = FilterName,
        f_any: str = FilterAny,
    ) -> List[UserModel]:
        return self.service.get_index(trans=trans, deleted=True, f_email=f_email, f_name=f_name, f_any=f_any)

    @router.post(
        "/api/users/deleted/{user_id}/undelete",
        name="undelete_user",
        summary="Restore the deleted user with the given `id`",
        require_admin=True,
    )
    def undelete(
        self, trans: ProvidesHistoryContext = DependsOnTrans, user_id: DecodedDatabaseIdField = UserIdPathParam
    ) -> DetailedUserModel:
        user = self.service.get_user(trans=trans, user_id=user_id)
        self.service.user_manager.undelete(user)
        return self.service.user_to_detailed_model(user)

    @router.get(
        "/api/users/deleted/{user_id}",
        name="get_deleted_user",
        summary="Display information about a deleted user",
    )
    def show_deleted(
        self,
        trans: ProvidesHistoryContext = DependsOnTrans,
        user_id: DecodedDatabaseIdField = UserIdPathParam,
    ) -> AnyUserModel:
        return self.service.show_user(trans=trans, user_id=user_id, deleted=True)

    @router.get(
        "/api/users/{user_id}/api_key",
        name="get_or_create_api_key",
        summary="Return the user's API key",
    )
    def get_or_create_api_key(
        self, trans: ProvidesUserContext = DependsOnTrans, user_id: DecodedDatabaseIdField = UserIdPathParam
    ) -> str:
        return self.service.get_or_create_api_key(trans, user_id)

    @router.get(
        "/api/users/{user_id}/api_key/detailed",
        name="get_api_key_detailed",
        summary="Return the user's API key with extra information.",
        responses={
            200: {
                "model": APIKeyModel,
                "description": "The API key of the user.",
            },
            204: {
                "description": "The user doesn't have an API key.",
            },
        },
    )
    def get_api_key(
        self, trans: ProvidesUserContext = DependsOnTrans, user_id: DecodedDatabaseIdField = UserIdPathParam
    ):
        api_key = self.service.get_api_key(trans, user_id)
        return api_key if api_key else Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.post("/api/users/{user_id}/api_key", summary="Creates a new API key for the user")
    def create_api_key(
        self, trans: ProvidesUserContext = DependsOnTrans, user_id: DecodedDatabaseIdField = UserIdPathParam
    ) -> str:
        return self.service.create_api_key(trans, user_id).key

    @router.delete(
        "/api/users/{user_id}/api_key",
        summary="Delete the current API key of the user",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    def delete_api_key(
        self,
        trans: ProvidesUserContext = DependsOnTrans,
        user_id: DecodedDatabaseIdField = UserIdPathParam,
    ):
        self.service.delete_api_key(trans, user_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.get(
        "/api/users/{user_id}/usage",
        name="get_user_usage",
        summary="Return the user's quota usage summary broken down by quota source",
    )
    def usage(
        self,
        trans: ProvidesUserContext = DependsOnTrans,
        user_id: FlexibleUserIdType = FlexibleUserIdPathParam,
    ) -> List[UserQuotaUsage]:
        user = self.service.get_user_full(trans, user_id, False)
        if user:
            rval = self.user_serializer.serialize_disk_usage(user)
            return rval
        else:
            return []

    @router.get(
        "/api/users/{user_id}/usage/{label}",
        name="get_user_usage_for_label",
        summary="Return the user's quota usage summary for a given quota source label",
    )
    def usage_for(
        self,
        trans: ProvidesUserContext = DependsOnTrans,
        user_id: FlexibleUserIdType = FlexibleUserIdPathParam,
        label: str = QuotaSourceLabelPathParam,
    ) -> Optional[UserQuotaUsage]:
        user = self.service.get_user_full(trans, user_id, False)
        effective_label: Optional[str] = label
        if label == "__null__":
            effective_label = None
        if user:
            rval = self.user_serializer.serialize_disk_usage_for(user, effective_label)
            return rval
        else:
            return None

    @router.get(
        "/api/users/{user_id}/beacon",
        summary="Returns information about beacon share settings",
    )
    def get_beacon(
        self,
        trans: ProvidesUserContext = DependsOnTrans,
        user_id: DecodedDatabaseIdField = UserIdPathParam,
    ) -> UserBeaconSetting:
        """
        **Warning**: This endpoint is experimental and might change or disappear in future versions.
        """
        user = self.service.get_user(trans, user_id)

        enabled = user.preferences["beacon_enabled"] if "beacon_enabled" in user.preferences else False

        return UserBeaconSetting(enabled=enabled)

    @router.post(
        "/api/users/{user_id}/beacon",
        summary="Changes beacon setting",
    )
    def set_beacon(
        self,
        trans: ProvidesUserContext = DependsOnTrans,
        user_id: DecodedDatabaseIdField = UserIdPathParam,
        payload: UserBeaconSetting = Body(...),
    ) -> UserBeaconSetting:
        """
        **Warning**: This endpoint is experimental and might change or disappear in future versions.
        """
        user = self.service.get_user(trans, user_id)

        user.preferences["beacon_enabled"] = payload.enabled
        with transaction(trans.sa_session):
            trans.sa_session.commit()

        return payload

    @router.put(
        "/api/users/{user_id}/theme/{theme}",
        summary="Sets the user's theme choice.",
    )
    def set_theme(
        self,
        trans: ProvidesUserContext = DependsOnTrans,
        user_id: DecodedDatabaseIdField = UserIdPathParam,
        theme: str = ThemePathParam,
    ) -> str:
        user = self.service.get_user(trans, user_id)
        user.preferences["theme"] = theme
        with transaction(trans.sa_session):
            trans.sa_session.commit()
        return theme

    @router.get("/api/users", name="Get users", description="Display a collection of users")
    def index(
        self,
        trans: ProvidesUserContext = DependsOnTrans,
        deleted: bool = UsersDeleted,
        f_email: str = FilterEmail,
        f_name: str = FilterName,
        f_any: str = FilterAny,
    ) -> List[UserModel]:
        return self.service.get_index(trans=trans, deleted=deleted, f_email=f_email, f_name=f_name, f_any=f_any)

    @router.post(
        "/api/users",
        name="Create user",
        summary="Creates a new Galaxy user.",
    )
    def create(
        self,
        trans: ProvidesUserContext = DependsOnTrans,
        payload: CreateUserPayload = CreateUserBody,
    ) -> CreatedUserModel:
        """
        Rework CreateUserPayload payload and add pydantic model for return
        """
        if not trans.app.config.allow_user_creation and not trans.user_is_admin:
            raise exceptions.ConfigDoesNotAllowException("User creation is not allowed in this Galaxy instance")

        # TODO: this is not working, because we cannot access get_or_create_remote_user
        if trans.app.config.use_remote_user and trans.user_is_admin:
            user = trans.get_or_create_remote_user(remote_user_email=payload.email)
        elif trans.user_is_admin:
            username = payload.username
            email = payload.email
            password = payload.password
            message = "\n".join(
                (
                    validate_email(trans, email),
                    validate_password(trans, password, password),
                    validate_publicname(trans, username),
                )
            ).rstrip()
            if message:
                raise exceptions.RequestParameterInvalidException(message)
            else:
                user = self.service.user_manager.create(email=email, username=username, password=password)
        else:
            raise exceptions.NotImplemented()
        item = user.to_dict(view="element", value_mapper={"id": trans.security.encode_id, "total_disk_usage": float})
        return item

    @router.get(
        "/api/users/current",
        name="get_current_user",
        description="Display information about current user",
    )
    @router.get(
        "/api/users/{user_id}",
        name="get_user",
        summary="Display information about a specified user",
    )
    def show(
        self,
        trans: ProvidesHistoryContext = DependsOnTrans,
        user_id: FlexibleUserIdType = FlexibleUserIdPathParam,
        deleted: Optional[bool] = UserDeleted,
    ) -> AnyUserModel:
        user_deleted = deleted or False
        return self.service.show_user(trans=trans, user_id=user_id, deleted=user_deleted)

    @router.put("/api/users/{user_id}", name="update_user", summary="Updates the values of the user")
    def update(
        self,
        payload: Dict[Any, Any] = UpdateUserBody,
        deleted: Optional[bool] = UserDeleted,
        trans: ProvidesUserContext = DependsOnTrans,
        user_id: DecodedDatabaseIdField = UserIdPathParam,
    ) -> DetailedUserModel:
        deleted = deleted or False
        current_user = trans.user
        user_to_update = self.service.get_user_full(trans, user_id, deleted=deleted)
        self.service.user_deserializer.deserialize(user_to_update, payload, user=current_user, trans=trans)
        return self.service.user_to_detailed_model(user_to_update)

    @router.delete(
        "/api/users/{user_id}",
        name="delete_user",
        summary="Delete the user with the given `id`, only admins can delete other users.",
    )
    def delete(
        self,
        payload: Optional[PurgeUserPayload] = PurgeUserBody,
        trans: ProvidesUserContext = DependsOnTrans,
        user_id: DecodedDatabaseIdField = UserIdPathParam,
    ) -> DetailedUserModel:
        user_to_update = self.service.user_manager.by_id(user_id)
        payload = payload or None
        if payload:
            purge = payload.purge
        else:
            purge = False
        if trans.user_is_admin:
            if purge:
                log.debug("Purging user %s", user_to_update)
                self.service.user_manager.purge(user_to_update)
            else:
                self.service.user_manager.delete(user_to_update)
        else:
            if trans.user == user_to_update:
                self.service.user_manager.delete(user_to_update)
            else:
                raise exceptions.InsufficientPermissionsException("You may only delete your own account.")
        return self.service.user_to_detailed_model(user_to_update)


class UserAPIController(BaseGalaxyAPIController, UsesTagsMixin, BaseUIController, UsesFormDefinitionsMixin):
    service: UsersService = depends(UsersService)
    user_manager: users.UserManager = depends(users.UserManager)
    user_serializer: users.UserSerializer = depends(users.UserSerializer)
    user_deserializer: users.UserDeserializer = depends(users.UserDeserializer)

    def _get_user_full(self, trans, user_id, **kwd):
        """Return referenced user or None if anonymous user is referenced."""
        deleted = kwd.get("deleted", "False")
        deleted = util.string_as_bool(deleted)
        return self.service.get_user_full(trans, user_id, deleted)

    def _get_extra_user_preferences(self, trans):
        """
        Reads the file user_preferences_extra_conf.yml to display
        admin defined user informations
        """
        return trans.app.config.user_preferences_extra["preferences"]

    def _build_extra_user_pref_inputs(self, trans, preferences, user):
        """
        Build extra user preferences inputs list.
        Add values to the fields if present
        """
        if not preferences:
            return []
        extra_pref_inputs = list()
        # Build sections for different categories of inputs
        user_vault = UserVaultWrapper(trans.app.vault, user)
        for item, value in preferences.items():
            if value is not None:
                input_fields = copy.deepcopy(value["inputs"])
                for input in input_fields:
                    help = input.get("help", "")
                    required = "Required" if util.string_as_bool(input.get("required")) else ""
                    if help:
                        input["help"] = f"{help} {required}"
                    else:
                        input["help"] = required
                    if input.get("store") == "vault":
                        field = f"{item}/{input['name']}"
                        input["value"] = user_vault.read_secret(f"preferences/{field}")
                    else:
                        field = f"{item}|{input['name']}"
                        for data_item in user.extra_preferences:
                            if field in data_item:
                                input["value"] = user.extra_preferences[data_item]
                    # regardless of the store, do not send secret type values to client
                    if input.get("type") == "secret":
                        input["value"] = "__SECRET_PLACEHOLDER__"
                        # let the client treat it as a password field
                        input["type"] = "password"
                extra_pref_inputs.append(
                    {
                        "type": "section",
                        "title": value["description"],
                        "name": item,
                        "expanded": True,
                        "inputs": input_fields,
                    }
                )
        return extra_pref_inputs

    @expose_api
    def get_information(self, trans, id, **kwd):
        """
        GET /api/users/{id}/information/inputs
        Return user details such as username, email, addresses etc.

        :param id: the encoded id of the user
        :type  id: str
        """
        user = self._get_user(trans, id)
        email = user.email
        username = user.username
        inputs = list()
        user_info = {
            "email": email,
            "username": username,
        }
        is_galaxy_app = trans.webapp.name == "galaxy"
        if trans.app.config.enable_account_interface or not is_galaxy_app:
            inputs.append(
                {
                    "id": "email_input",
                    "name": "email",
                    "type": "text",
                    "label": "Email address",
                    "value": email,
                    "help": "If you change your email address you will receive an activation link in the new mailbox and you have to activate your account by visiting it.",
                }
            )
        if is_galaxy_app:
            if trans.app.config.enable_account_interface:
                inputs.append(
                    {
                        "id": "name_input",
                        "name": "username",
                        "type": "text",
                        "label": "Public name",
                        "value": username,
                        "help": 'Your public name is an identifier that will be used to generate addresses for information you share publicly. Public names must be at least three characters in length and contain only lower-case letters, numbers, dots, underscores, and dashes (".", "_", "-").',
                    }
                )
            info_form_models = self.get_all_forms(
                trans, filter=dict(deleted=False), form_type=trans.app.model.FormDefinition.types.USER_INFO
            )
            if info_form_models:
                info_form_id = trans.security.encode_id(user.values.form_definition.id) if user.values else None
                info_field = {
                    "type": "conditional",
                    "name": "info",
                    "cases": [],
                    "test_param": {
                        "name": "form_id",
                        "label": "User type",
                        "type": "select",
                        "value": info_form_id,
                        "help": "",
                        "data": [],
                    },
                }
                for f in info_form_models:
                    values = None
                    if info_form_id == trans.security.encode_id(f.id) and user.values:
                        values = user.values.content
                    info_form = f.to_dict(user=user, values=values, security=trans.security)
                    info_field["test_param"]["data"].append({"label": info_form["name"], "value": info_form["id"]})
                    info_field["cases"].append({"value": info_form["id"], "inputs": info_form["inputs"]})
                inputs.append(info_field)

            if trans.app.config.enable_account_interface:
                address_inputs = [{"type": "hidden", "name": "id", "hidden": True}]
                for field in AddressField.fields():
                    address_inputs.append({"type": "text", "name": field[0], "label": field[1], "help": field[2]})
                address_repeat = {
                    "title": "Address",
                    "name": "address",
                    "type": "repeat",
                    "inputs": address_inputs,
                    "cache": [],
                }
                address_values = [address.to_dict(trans) for address in user.addresses]
                for address in address_values:
                    address_cache = []
                    for input in address_inputs:
                        input_copy = input.copy()
                        input_copy["value"] = address.get(input["name"])
                        address_cache.append(input_copy)
                    address_repeat["cache"].append(address_cache)
                inputs.append(address_repeat)
                user_info["addresses"] = [address.to_dict(trans) for address in user.addresses]

            # Build input sections for extra user preferences
            extra_user_pref = self._build_extra_user_pref_inputs(trans, self._get_extra_user_preferences(trans), user)
            for item in extra_user_pref:
                inputs.append(item)
        else:
            if user.active_repositories:
                inputs.append(
                    dict(
                        id="name_input",
                        name="username",
                        label="Public name:",
                        type="hidden",
                        value=username,
                        help="You cannot change your public name after you have created a repository in this tool shed.",
                    )
                )
            else:
                inputs.append(
                    dict(
                        id="name_input",
                        name="username",
                        label="Public name:",
                        type="text",
                        value=username,
                        help='Your public name provides a means of identifying you publicly within this tool shed. Public names must be at least three characters in length and contain only lower-case letters, numbers, dots, underscores, and dashes (".", "_", "-"). You cannot change your public name after you have created a repository in this tool shed.',
                    )
                )
        user_info["inputs"] = inputs
        return user_info

    @expose_api
    def set_information(self, trans, id, payload=None, **kwd):
        """
        PUT /api/users/{id}/information/inputs
        Save a user's email, username, addresses etc.

        :param id: the encoded id of the user
        :type  id: str

        :param payload: data with new settings
        :type  payload: dict
        """
        payload = payload or {}
        user = self._get_user(trans, id)
        # Update email
        if "email" in payload:
            email = payload.get("email")
            message = validate_email(trans, email, user)
            if message:
                raise exceptions.RequestParameterInvalidException(message)
            if user.email != email:
                # Update user email and user's private role name which must match
                private_role = trans.app.security_agent.get_private_user_role(user)
                private_role.name = email
                private_role.description = f"Private role for {email}"
                user.email = email
                trans.sa_session.add(user)
                trans.sa_session.add(private_role)
                with transaction(trans.sa_session):
                    trans.sa_session.commit()
                if trans.app.config.user_activation_on:
                    # Deactivate the user if email was changed and activation is on.
                    user.active = False
                    if self.user_manager.send_activation_email(trans, user.email, user.username):
                        message = "The login information has been updated with the changes.<br>Verification email has been sent to your new email address. Please verify it by clicking the activation link in the email.<br>Please check your spam/trash folder in case you cannot find the message."
                    else:
                        message = "Unable to send activation email, please contact your local Galaxy administrator."
                        if trans.app.config.error_email_to is not None:
                            message += f" Contact: {trans.app.config.error_email_to}"
                        raise exceptions.InternalServerError(message)
        # Update public name
        if "username" in payload:
            username = payload.get("username")
            message = validate_publicname(trans, username, user)
            if message:
                raise exceptions.RequestParameterInvalidException(message)
            if user.username != username:
                user.username = username
        # Update user custom form
        user_info_form_id = payload.get("info|form_id")
        if user_info_form_id:
            prefix = "info|"
            user_info_form = trans.sa_session.query(trans.app.model.FormDefinition).get(
                trans.security.decode_id(user_info_form_id)
            )
            user_info_values = {}
            for item in payload:
                if item.startswith(prefix):
                    user_info_values[item[len(prefix) :]] = payload[item]
            form_values = trans.model.FormValues(user_info_form, user_info_values)
            trans.sa_session.add(form_values)
            user.values = form_values

        # Update values for extra user preference items
        extra_user_pref_data = dict()
        extra_pref_keys = self._get_extra_user_preferences(trans)
        user_vault = UserVaultWrapper(trans.app.vault, user)
        if extra_pref_keys is not None:
            for key in extra_pref_keys:
                key_prefix = f"{key}|"
                for item in payload:
                    if item.startswith(key_prefix):
                        keys = item.split("|")
                        section = extra_pref_keys[keys[0]]
                        matching_input = [input for input in section["inputs"] if input["name"] == keys[1]]
                        if matching_input:
                            input = matching_input[0]
                            if input.get("required") and payload[item] == "":
                                raise exceptions.ObjectAttributeMissingException("Please fill the required field")
                            if not (input.get("type") == "secret" and payload[item] == "__SECRET_PLACEHOLDER__"):
                                if input.get("store") == "vault":
                                    user_vault.write_secret(f"preferences/{keys[0]}/{keys[1]}", str(payload[item]))
                                else:
                                    extra_user_pref_data[item] = payload[item]
                        else:
                            extra_user_pref_data[item] = payload[item]
            user.preferences["extra_user_preferences"] = json.dumps(extra_user_pref_data)

        # Update user addresses
        address_dicts = {}
        address_count = 0
        for item in payload:
            match = re.match(r"^address_(?P<index>\d+)\|(?P<attribute>\S+)", item)
            if match:
                groups = match.groupdict()
                index = int(groups["index"])
                attribute = groups["attribute"]
                address_dicts[index] = address_dicts.get(index) or {}
                address_dicts[index][attribute] = payload[item]
                address_count = max(address_count, index + 1)
        user.addresses = []
        for index in range(0, address_count):
            d = address_dicts[index]
            if d.get("id"):
                try:
                    user_address = trans.sa_session.query(UserAddress).get(trans.security.decode_id(d["id"]))
                except Exception as e:
                    raise exceptions.ObjectNotFound(f"Failed to access user address ({d['id']}). {e}")
            else:
                user_address = UserAddress()
                trans.log_event("User address added")
            for field in AddressField.fields():
                if str(field[2]).lower() == "required" and not d.get(field[0]):
                    raise exceptions.ObjectAttributeMissingException(
                        f"Address {index + 1}: {field[1]} ({field[0]}) required."
                    )
                setattr(user_address, field[0], str(d.get(field[0], "")))
            user_address.user = user
            user.addresses.append(user_address)
            trans.sa_session.add(user_address)
        trans.sa_session.add(user)
        with transaction(trans.sa_session):
            trans.sa_session.commit()
        trans.log_event("User information added")
        return {"message": "User information has been saved."}

    @expose_api
    def set_favorite(self, trans, id, object_type, payload=None, **kwd):
        """Add the object to user's favorites
        PUT /api/users/{id}/favorites/{object_type}

        :param id: the encoded id of the user
        :type  id: str
        :param object_type: the object type that users wants to favorite
        :type  object_type: str
        :param object_id: the id of an object that users wants to favorite
        :type  object_id: str
        """
        payload = payload or {}
        self._validate_favorite_object_type(object_type)
        user = self._get_user(trans, id)
        favorites = json.loads(user.preferences["favorites"]) if "favorites" in user.preferences else {}
        if object_type == "tools":
            tool_id = payload.get("object_id")
            tool = self.app.toolbox.get_tool(tool_id)
            if not tool:
                raise exceptions.ObjectNotFound(f"Could not find tool with id '{tool_id}'.")
            if not tool.allow_user_access(user):
                raise exceptions.AuthenticationFailed(f"Access denied for tool with id '{tool_id}'.")
            if "tools" in favorites:
                favorite_tools = favorites["tools"]
            else:
                favorite_tools = []
            if tool_id not in favorite_tools:
                favorite_tools.append(tool_id)
                favorites["tools"] = favorite_tools
                user.preferences["favorites"] = json.dumps(favorites)
                with transaction(trans.sa_session):
                    trans.sa_session.commit()
        return favorites

    @expose_api
    def remove_favorite(self, trans, id, object_type, object_id, payload=None, **kwd):
        """Remove the object from user's favorites
        DELETE /api/users/{id}/favorites/{object_type}/{object_id:.*?}

        :param id: the encoded id of the user
        :type  id: str
        :param object_type: the object type that users wants to favorite
        :type  object_type: str
        :param object_id: the id of an object that users wants to remove from favorites
        :type  object_id: str
        """
        payload = payload or {}
        self._validate_favorite_object_type(object_type)
        user = self._get_user(trans, id)
        favorites = json.loads(user.preferences["favorites"]) if "favorites" in user.preferences else {}
        if object_type == "tools":
            if "tools" in favorites:
                favorite_tools = favorites["tools"]
                if object_id in favorite_tools:
                    del favorite_tools[favorite_tools.index(object_id)]
                    favorites["tools"] = favorite_tools
                    user.preferences["favorites"] = json.dumps(favorites)
                    with transaction(trans.sa_session):
                        trans.sa_session.commit()
                else:
                    raise exceptions.ObjectNotFound("Given object is not in the list of favorites")
        return favorites

    def _validate_favorite_object_type(self, object_type):
        if object_type in ["tools"]:
            pass
        else:
            raise exceptions.ObjectAttributeInvalidException(
                f"This type is not supported. Given object_type: {object_type}"
            )

    @expose_api
    def get_password(self, trans, id, payload=None, **kwd):
        """
        Return available password inputs.
        """
        payload = payload or {}
        return {
            "inputs": [
                {"name": "current", "type": "password", "label": "Current password"},
                {"name": "password", "type": "password", "label": "New password"},
                {"name": "confirm", "type": "password", "label": "Confirm password"},
            ]
        }

    @expose_api
    def set_password(self, trans, id, payload=None, **kwd):
        """
        Allows to the logged-in user to change own password.
        """
        payload = payload or {}
        user, message = self.user_manager.change_password(trans, id=id, **payload)
        if user is None:
            raise exceptions.AuthenticationRequired(message)
        return {"message": "Password has been changed."}

    @expose_api
    def get_permissions(self, trans, id, payload=None, **kwd):
        """
        Get the user's default permissions for the new histories
        """
        payload = payload or {}
        user = self._get_user(trans, id)
        roles = user.all_roles()
        inputs = []
        for index, action in trans.app.model.Dataset.permitted_actions.items():
            inputs.append(
                {
                    "type": "select",
                    "multiple": True,
                    "optional": True,
                    "name": index,
                    "label": action.action,
                    "help": action.description,
                    "options": list({(r.name, r.id) for r in roles}),
                    "value": [a.role.id for a in user.default_permissions if a.action == action.action],
                }
            )
        return {"inputs": inputs}

    @expose_api
    def set_permissions(self, trans, id, payload=None, **kwd):
        """
        Set the user's default permissions for the new histories
        """
        payload = payload or {}
        user = self._get_user(trans, id)
        permissions = {}
        for index, action in trans.app.model.Dataset.permitted_actions.items():
            action_id = trans.app.security_agent.get_action(action.action).action
            permissions[action_id] = [
                trans.sa_session.query(trans.app.model.Role).get(x) for x in (payload.get(index) or [])
            ]
        trans.app.security_agent.user_set_default_permissions(user, permissions)
        return {"message": "Permissions have been saved."}

    @expose_api
    def get_toolbox_filters(self, trans, id, payload=None, **kwd):
        """
        API call for fetching toolbox filters data. Toolbox filters are specified in galaxy.ini.
        The user can activate them and the choice is stored in user_preferences.
        """
        payload = payload or {}
        user = self._get_user(trans, id)
        filter_types = self._get_filter_types(trans)
        saved_values = {}
        for name, value in user.preferences.items():
            if name in filter_types:
                saved_values[name] = listify(value, do_strip=True)
        inputs = [
            {
                "type": "hidden",
                "name": "helptext",
                "label": "In this section you may enable or disable Toolbox filters. Please contact your admin to configure filters as necessary.",
            }
        ]
        errors = {}
        factory = FilterFactory(trans.app.toolbox)
        for filter_type in filter_types:
            self._add_filter_inputs(factory, filter_types, inputs, errors, filter_type, saved_values)
        return {"inputs": inputs, "errors": errors}

    @expose_api
    def set_toolbox_filters(self, trans, id, payload=None, **kwd):
        """
        API call to update toolbox filters data.
        """
        payload = payload or {}
        user = self._get_user(trans, id)
        filter_types = self._get_filter_types(trans)
        for filter_type in filter_types:
            new_filters = []
            for prefixed_name in payload:
                if prefixed_name.startswith(filter_type):
                    filter_selection = payload.get(prefixed_name)
                    if type(filter_selection) != bool:
                        raise exceptions.RequestParameterInvalidException(
                            "Please specify the filter selection as boolean value."
                        )
                    if filter_selection:
                        prefix = f"{filter_type}|"
                        new_filters.append(prefixed_name[len(prefix) :])
            user.preferences[filter_type] = ",".join(new_filters)
        trans.sa_session.add(user)
        with transaction(trans.sa_session):
            trans.sa_session.commit()
        return {"message": "Toolbox filters have been saved."}

    def _add_filter_inputs(self, factory, filter_types, inputs, errors, filter_type, saved_values):
        filter_inputs = list()
        filter_values = saved_values.get(filter_type, [])
        filter_config = filter_types[filter_type]["config"]
        filter_title = filter_types[filter_type]["title"]
        for filter_name in filter_config:
            function = factory.build_filter_function(filter_name)
            if function is None:
                errors[f"{filter_type}|{filter_name}"] = "Filter function not found."

            short_description, description = None, None
            doc_string = docstring_trim(function.__doc__)
            split = doc_string.split("\n\n")
            if split:
                short_description = split[0]
                if len(split) > 1:
                    description = split[1]
            else:
                log.warning(f"No description specified in the __doc__ string for {filter_name}.")

            filter_inputs.append(
                {
                    "type": "boolean",
                    "name": filter_name,
                    "label": short_description or filter_name,
                    "help": description or "No description available.",
                    "value": True if filter_name in filter_values else False,
                }
            )
        if filter_inputs:
            inputs.append(
                {
                    "type": "section",
                    "title": filter_title,
                    "name": filter_type,
                    "expanded": True,
                    "inputs": filter_inputs,
                }
            )

    def _get_filter_types(self, trans):
        return {
            "toolbox_tool_filters": {"title": "Tools", "config": trans.app.config.user_tool_filters},
            "toolbox_section_filters": {"title": "Sections", "config": trans.app.config.user_tool_section_filters},
            "toolbox_label_filters": {"title": "Labels", "config": trans.app.config.user_tool_label_filters},
        }

    @expose_api
    def get_custom_builds(self, trans, id, payload=None, **kwd):
        """
        GET /api/users/{id}/custom_builds
        Returns collection of custom builds.

        :param id: the encoded id of the user
        :type  id: str
        """
        payload = payload or {}
        user = self._get_user(trans, id)
        dbkeys = json.loads(user.preferences["dbkeys"]) if "dbkeys" in user.preferences else {}
        valid_dbkeys = {}
        update = False
        for key, dbkey in dbkeys.items():
            if "count" not in dbkey and "linecount" in dbkey:
                chrom_count_dataset = trans.sa_session.query(trans.app.model.HistoryDatasetAssociation).get(
                    dbkey["linecount"]
                )
                if (
                    chrom_count_dataset
                    and not chrom_count_dataset.deleted
                    and chrom_count_dataset.state == trans.app.model.HistoryDatasetAssociation.states.OK
                ):
                    chrom_count = int(open(chrom_count_dataset.file_name).readline())
                    dbkey["count"] = chrom_count
                    valid_dbkeys[key] = dbkey
                    update = True
            else:
                valid_dbkeys[key] = dbkey
        if update:
            user.preferences["dbkeys"] = json.dumps(valid_dbkeys)
        dbkey_collection = []
        for key, attributes in valid_dbkeys.items():
            attributes["id"] = key
            dbkey_collection.append(attributes)
        return dbkey_collection

    @expose_api
    def add_custom_builds(self, trans, id, key, payload=None, **kwd):
        """
        PUT /api/users/{id}/custom_builds/{key}
        Add new custom build.

        :param id: the encoded id of the user
        :type  id: str

        :param id: custom build key
        :type  id: str

        :param payload: data with new build details
        :type  payload: dict
        """
        payload = payload or {}
        user = self._get_user(trans, id)
        dbkeys = json.loads(user.preferences["dbkeys"]) if "dbkeys" in user.preferences else {}
        name = payload.get("name")
        len_type = payload.get("len|type")
        len_value = payload.get("len|value")
        if len_type not in ["file", "fasta", "text"] or not len_value:
            raise exceptions.RequestParameterInvalidException("Please specify a valid data source type.")
        if not name or not key:
            raise exceptions.RequestParameterMissingException("You must specify values for all the fields.")
        elif key in dbkeys:
            raise exceptions.DuplicatedIdentifierException(
                "There is already a custom build with that key. Delete it first if you want to replace it."
            )
        else:
            # Have everything needed; create new build.
            build_dict = {"name": name}
            if len_type in ["text", "file"]:
                # Create new len file
                new_len = trans.app.model.HistoryDatasetAssociation(
                    extension="len", create_dataset=True, sa_session=trans.sa_session
                )
                trans.sa_session.add(new_len)
                new_len.name = name
                new_len.visible = False
                new_len.state = trans.app.model.Job.states.OK
                new_len.info = "custom build .len file"
                try:
                    trans.app.object_store.create(new_len.dataset)
                except ObjectInvalid:
                    raise exceptions.InternalServerError("Unable to create output dataset: object store is full.")
                with transaction(trans.sa_session):
                    trans.sa_session.commit()
                counter = 0
                lines_skipped = 0
                with open(new_len.file_name, "w") as f:
                    # LEN files have format:
                    #   <chrom_name><tab><chrom_length>
                    for line in len_value.split("\n"):
                        # Splits at the last whitespace in the line
                        lst = line.strip().rsplit(None, 1)
                        if not lst or len(lst) < 2:
                            lines_skipped += 1
                            continue
                        chrom, length = lst[0], lst[1]
                        try:
                            length = int(length)
                        except ValueError:
                            lines_skipped += 1
                            continue
                        if chrom != escape(chrom):
                            build_dict["message"] = "Invalid chromosome(s) with HTML detected and skipped."
                            lines_skipped += 1
                            continue
                        counter += 1
                        f.write(f"{chrom}\t{length}\n")
                build_dict["len"] = new_len.id
                build_dict["count"] = counter
            else:
                build_dict["fasta"] = trans.security.decode_id(len_value)
                dataset = trans.sa_session.query(trans.app.model.HistoryDatasetAssociation).get(build_dict["fasta"])
                try:
                    new_len = dataset.get_converted_dataset(trans, "len")
                    new_linecount = new_len.get_converted_dataset(trans, "linecount")
                    build_dict["len"] = new_len.id
                    build_dict["linecount"] = new_linecount.id
                except Exception:
                    raise exceptions.ToolExecutionError("Failed to convert dataset.")
            dbkeys[key] = build_dict
            user.preferences["dbkeys"] = json.dumps(dbkeys)
            with transaction(trans.sa_session):
                trans.sa_session.commit()
            return build_dict

    @expose_api
    def delete_custom_builds(self, trans, id, key, payload=None, **kwd):
        """
        DELETE /api/users/{id}/custom_builds/{key}
        Delete a custom build.

        :param id: the encoded id of the user
        :type  id: str

        :param id: custom build key to be deleted
        :type  id: str
        """
        payload = payload or {}
        user = self._get_user(trans, id)
        dbkeys = json.loads(user.preferences["dbkeys"]) if "dbkeys" in user.preferences else {}
        if key and key in dbkeys:
            del dbkeys[key]
            user.preferences["dbkeys"] = json.dumps(dbkeys)
            with transaction(trans.sa_session):
                trans.sa_session.commit()
            return {"message": f"Deleted {key}."}
        else:
            raise exceptions.ObjectNotFound(f"Could not find and delete build ({key}).")

    def _get_user(self, trans, id):
        user = self.get_user(trans, id)
        if not user:
            raise exceptions.RequestParameterInvalidException("Invalid user id specified.")
        if user != trans.user and not trans.user_is_admin:
            raise exceptions.InsufficientPermissionsException("Access denied.")
        return user
