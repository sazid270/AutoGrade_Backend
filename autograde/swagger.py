from drf_yasg.inspectors import SwaggerAutoSchema


class CustomAutoSchema(SwaggerAutoSchema):
    def get_tags(self, operation_keys=None):
        tags = self.overrides.get("tags", None) or getattr(
            self.view, "swagger_tags", []
        )
        if not tags:
            if operation_keys:
                tags = [operation_keys[0]]
            else:
                # Fallback to view class name when operation_keys is None
                tags = [self.view.__class__.__name__]

        return ["swagger_" + tag for tag in tags]
