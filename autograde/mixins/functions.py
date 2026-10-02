import logging

from django.conf import settings
from django.core.mail import EmailMessage
from django.template.loader import render_to_string
from drf_yasg import openapi
from drf_yasg.utils import swagger_auto_schema

from autograde.mixins.classes import CustomUUIDFactory


# Utility function to send HTML email
def send_html_email(template_name, context, subject, to):
    email_html_message = render_to_string(template_name, context)
    email = EmailMessage(
        subject=subject,
        body=email_html_message,
        from_email=settings.EMAIL_HOST_USER,
        to=[to],
    )
    email.content_subtype = "html"
    try:
        email.send()
    except Exception as e:
        logging.error(f"Failed to send email ({template_name}). Error: {e}")


# Utility function to generate a custom UUID
def get_custom_uuid(model_name: str) -> CustomUUIDFactory:
    """
    Generate a custom UUID in the format AG-{model_name}-xxxxxxxx
    where xxxxxxxx is an 8-character hexadecimal string.
    """

    return CustomUUIDFactory(model_name)


# Utility function to create swagger schema decorator with query parameters
def create_swagger_schema(parameters):
    manual_parameters = []

    for param in parameters:
        parameter_obj = openapi.Parameter(
            name=param.get("name"),
            in_=param.get("location", openapi.IN_QUERY),
            description=param.get("description", ""),
            type=param.get("type", openapi.TYPE_STRING),
            required=param.get("required", False),
        )
        manual_parameters.append(parameter_obj)

    return swagger_auto_schema(manual_parameters=manual_parameters)
