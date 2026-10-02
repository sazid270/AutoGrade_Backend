import uuid

from django.utils.deconstruct import deconstructible
from drf_yasg import openapi
from drf_yasg.utils import swagger_auto_schema
from rest_framework import authentication, generics, status
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework_simplejwt import authentication as jwt_authentication


# Define a custom pagination class
class CustomPagePagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    page_size_query_description = (
        "Number of results to return per page. Default is 20, maximum is 1000."
    )
    max_page_size = 1000

    def get_page_size(self, request):
        try:
            page_size = int(request.query_params.get("page_size", self.page_size))
            if page_size > self.max_page_size:
                page_size = self.max_page_size
            elif page_size < 1:
                page_size = self.page_size
            return page_size
        except (ValueError, TypeError):
            return self.page_size


# Handle swagger fake views
class SwaggerFakeView:
    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            if self.queryset is not None:
                return self.queryset.none()
            return []
        return super().get_queryset()


# Generate custom UUIDs
@deconstructible
class CustomUUIDFactory:
    def __init__(self, model_name: str):
        self.model_name = model_name

    def __call__(self) -> str:
        hex_part = uuid.uuid4().hex[:8]
        return f"AG-{self.model_name}-{hex_part}"


# Create multiple items
class BulkCreateView(SwaggerFakeView, generics.CreateAPIView):
    queryset = None
    serializer_class = None
    authentication_classes = [
        jwt_authentication.JWTAuthentication,
        authentication.SessionAuthentication,
    ]
    permission_classes = None
    unique_fields = None
    swagger_tags = None

    def get_serializer(self, *args, **kwargs):
        # Override serializer to support many=True for array input in Swagger documentation
        serializer_class = self.get_serializer_class()
        kwargs.setdefault("context", self.get_serializer_context())

        # For non-POST requests or when not instantiating, return normal serializer
        if self.request.method != "POST":
            return serializer_class(*args, **kwargs)

        # Return serializer with many=True for POST requests
        kwargs["many"] = True
        return serializer_class(*args, **kwargs)

    def post(self, request):
        data = request.data
        if not data:
            return Response(
                {"detail": "No data provided."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Require array input
        if not isinstance(data, list):
            return Response(
                {"detail": "Request body must be an array of objects."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        created_objects = []
        failed_items = []
        skipped_items = []
        created_count = 0

        # Track items to process, filtering out duplicates and existing items
        items_to_process = []
        seen_in_request = {}  # Track items seen in this request

        for idx, item in enumerate(data):
            skip_reason = None

            # Check for duplicates within the request array
            if self.unique_fields:
                for field in self.unique_fields:
                    value = item.get(field)
                    if value is not None:
                        key = (field, value)
                        if key in seen_in_request:
                            skip_reason = {
                                "field": field,
                                "value": value,
                                "reason": f"Duplicate {field} found in request at index {seen_in_request[key]}",
                            }
                            break
                        seen_in_request[key] = idx

            # Check if item already exists in database
            if not skip_reason and self.unique_fields:
                model = self.queryset.model
                filter_kwargs = {}
                for field in self.unique_fields:
                    value = item.get(field)
                    if value is not None:
                        filter_kwargs[field] = value

                if filter_kwargs and model.objects.filter(**filter_kwargs).exists():
                    skip_reason = {
                        "fields": self.unique_fields,
                        "values": {f: item.get(f) for f in self.unique_fields},
                        "reason": "Item with these values already exists in database",
                    }

            if skip_reason:
                skipped_items.append(
                    {
                        "index": idx,
                        "data": item,
                        "skip_reason": skip_reason,
                    }
                )
            else:
                items_to_process.append((idx, item))

        # Process non-skipped items
        for idx, item in items_to_process:
            # Pre-create callback check
            pre_create_error = None
            if hasattr(self, "pre_create_item"):
                pre_create_error = self.pre_create_item(item)
            if pre_create_error:
                failed_items.append(
                    {
                        "index": idx,
                        "data": item,
                        "errors": {"detail": pre_create_error},
                    }
                )
                continue

            serializer = self.get_serializer_class()(data=item)

            # Check if serializer is valid
            if not serializer.is_valid():
                failed_items.append(
                    {
                        "index": idx,
                        "data": item,
                        "errors": serializer.errors,
                    }
                )
                continue

            # Check object-level permissions before creation
            try:
                # Create the object first to check permissions
                obj = serializer.save()

                # Check permissions on the created object
                if all(
                    p.has_object_permission(request, self, obj)
                    for p in self.get_permissions()
                ):
                    created_objects.append(serializer.data)
                    created_count += 1
                else:
                    # Delete if permission check fails
                    obj.delete()
                    failed_items.append(
                        {
                            "index": idx,
                            "data": item,
                            "errors": {
                                "detail": "You don't have permission to create this object."
                            },
                        }
                    )
            except Exception as e:
                failed_items.append(
                    {
                        "index": idx,
                        "data": item,
                        "errors": {"detail": str(e)},
                    }
                )

        # If no objects were created, return 400
        if created_count == 0:
            return Response(
                {
                    "detail": "No objects were created.",
                    "failed_items": failed_items if failed_items else None,
                    "skipped_items": skipped_items if skipped_items else None,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        response_body = {
            "created_count": created_count,
            "created_objects": created_objects,
        }
        if failed_items:
            response_body["failed_items"] = failed_items
        if skipped_items:
            response_body["skipped_items"] = skipped_items

        response_status = status.HTTP_201_CREATED
        if failed_items or skipped_items:
            response_status = status.HTTP_207_MULTI_STATUS

        # Add a human-readable success message
        if created_count:
            response_body["detail"] = (
                f"{created_count} object{'s' if created_count != 1 else ''} created successfully."
            )

        return Response(response_body, status=response_status)


# Delete multiple items
class BulkDeleteView(SwaggerFakeView, generics.DestroyAPIView):
    queryset = None
    authentication_classes = [
        jwt_authentication.JWTAuthentication,
        authentication.SessionAuthentication,
    ]
    permission_classes = None
    swagger_tags = None

    @swagger_auto_schema(
        manual_parameters=[
            openapi.Parameter(
                "ids",
                openapi.IN_QUERY,
                description="Comma-separated list of IDs to delete",
                type=openapi.TYPE_STRING,
                required=True,
            ),
        ]
    )
    def delete(self, request):
        ids = request.query_params.get("ids")
        if not ids:
            return Response(
                {"detail": "No IDs provided."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        id_list = [i.strip() for i in ids.split(",") if i.strip()]
        model = self.queryset.model
        # Detect primary key field name (could be id, user_id, product_id, etc.)
        pk_field = model._meta.pk.name

        # Fetch the objects first
        objects = model.objects.filter(**{f"{pk_field}__in": id_list})

        # Build lists for found / not found ids
        found_ids = [str(o.pk) for o in objects]
        not_found_ids = [i for i in id_list if i not in found_ids]

        # If no objects were found at all, return 404
        if not objects:
            return Response(
                {
                    "detail": "No objects found for the provided IDs.",
                    "requested_ids": id_list,
                    "not_found_ids": id_list,
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        # Check object-level permissions
        allowed_objects = []
        for obj in objects:
            if all(
                p.has_object_permission(request, self, obj)
                for p in self.get_permissions()
            ):
                allowed_objects.append(obj.pk)

        # If objects exist but user has permission for none of them, return 403
        if not allowed_objects:
            return Response(
                {
                    "detail": "You don't have permission to delete the objects.",
                    "found_ids": found_ids,
                    "not_allowed_ids": found_ids,
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        # Delete only allowed objects
        qs_to_delete = model.objects.filter(**{f"{pk_field}__in": allowed_objects})
        # Capture the primary keys that will be deleted before performing delete
        deleted_ids_predelete = []
        deleted_count = 0
        for obj in qs_to_delete:
            deleted_ids_predelete.append(str(obj.pk))
            obj.delete()
            self.post_delete_hook(obj, request)
            deleted_count += 1

        # Prepare response with context about partial successes/failures
        not_allowed_ids = [
            fid for fid in found_ids if fid not in [str(a) for a in allowed_objects]
        ]

        response_body = {
            "deleted_count": deleted_count,
            "deleted_ids": deleted_ids_predelete,
        }
        if not_found_ids:
            response_body["not_found_ids"] = not_found_ids
        if not_allowed_ids:
            response_body["not_allowed_ids"] = not_allowed_ids

        # Add a human-readable success message when deletions occurred
        if deleted_count:
            response_body["detail"] = (
                f"{deleted_count} object{'s' if deleted_count != 1 else ''} deleted successfully."
            )

        return Response(response_body, status=status.HTTP_200_OK)

    def post_delete_hook(self, obj, request):
        """Optional hook to run custom logic after each object is deleted."""
        pass
