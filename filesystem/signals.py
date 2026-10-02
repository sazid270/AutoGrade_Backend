import logging

from django.contrib.auth import get_user_model
from django.db import models, transaction
from django.db.models.signals import post_delete, pre_delete
from django.dispatch import receiver

from filesystem.models.models import FileModel, ImageModel
from filesystem.utils import delete_file_from_storage

User = get_user_model()
logger = logging.getLogger(__name__)


# Clean up all files and images when a user is deleted
@receiver(pre_delete, sender=User)
def cleanup_user_files(sender, instance, **kwargs):
    # Delete all files uploaded by this user
    user_files = FileModel.objects.filter(created_by=instance)
    files_count = user_files.count()
    if files_count > 0:
        logger.info(f"Deleting {files_count} files created by user {instance.email}")
        user_files.delete()

    # Delete all images uploaded by this user
    user_images = ImageModel.objects.filter(created_by=instance)
    images_count = user_images.count()
    if images_count > 0:
        logger.info(f"Checking {images_count} images created by user {instance.email}")
        deleted_count = 0
        for image in user_images:
            # Check if this image is referenced by models other than the user being deleted
            is_referenced = False
            for rel in image._meta.related_objects:
                accessor_name = rel.get_accessor_name()
                if not accessor_name:
                    continue
                try:
                    related_manager = getattr(image, accessor_name)
                    if hasattr(related_manager, "exists") and related_manager.exists():
                        # Check if references belong to other users or the current user
                        if hasattr(related_manager.first(), "user"):
                            # If related objects belong to other users, keep the image
                            other_user_refs = related_manager.exclude(
                                user=instance
                            ).exists()
                            if other_user_refs:
                                is_referenced = True
                                break
                        elif hasattr(related_manager.first(), "created_by"):
                            # If related objects created by other users, keep the image
                            other_user_refs = related_manager.exclude(
                                created_by=instance
                            ).exists()
                            if other_user_refs:
                                is_referenced = True
                                break
                        else:
                            # If we can't determine ownership, assume it's referenced
                            is_referenced = True
                            break
                except Exception as e:
                    logger.debug(f"Error checking relation {accessor_name}: {e}")
                    continue

            if not is_referenced:
                image.delete()
                deleted_count += 1
            else:
                logger.debug(
                    f"Image {getattr(image, 'id', 'unknown')} is still referenced by other users, preserving"
                )

        logger.info(
            f"Deleted {deleted_count} orphaned images for user {instance.email}"
        )


# Clean up file storage when FileModel instance is deleted
@receiver(post_delete, sender=FileModel)
def cleanup_file_on_delete(sender, instance, **kwargs):
    if instance.file:
        delete_file_from_storage(instance.file.name)


# Clean up image storage when ImageModel instance is deleted
@receiver(post_delete, sender=ImageModel)
def cleanup_image_on_delete(sender, instance, **kwargs):
    if instance.image:
        delete_file_from_storage(instance.image.name)


# Automatically delete orphaned ImageModel records when related models are deleted
@receiver(pre_delete)
def mark_orphaned_images_for_deletion(sender, instance, **kwargs):
    # Skip if the sender is ImageModel itself (avoid recursion)
    if sender == ImageModel:
        return

    # Look for any ImageModel foreign keys in the instance being deleted
    for field in instance._meta.get_fields():
        # Check if this is a ForeignKey to ImageModel
        if isinstance(field, models.ForeignKey) and field.related_model == ImageModel:
            try:
                image = getattr(instance, field.name, None)
                if not image:
                    continue

                # Check if this image will be orphaned after deletion
                is_referenced = False

                # Get all reverse relations to ImageModel
                for rel in image._meta.related_objects:
                    accessor_name = rel.get_accessor_name()
                    if not accessor_name:
                        continue
                    try:
                        related_manager = getattr(image, accessor_name)

                        # Exclude the current instance being deleted
                        if hasattr(related_manager, "exclude"):
                            remaining = related_manager.exclude(pk=instance.pk)
                            if remaining.exists():
                                is_referenced = True
                                break
                        elif hasattr(related_manager, "count"):
                            # For reverse OneToOne relations
                            if related_manager != instance:
                                is_referenced = True
                                break
                    except Exception as e:
                        logger.debug(
                            f"Error checking relation {accessor_name} for image {getattr(image, 'id', 'unknown')}: {e}"
                        )
                        continue

                # If no other references exist, mark for deletion
                if not is_referenced:
                    if not hasattr(instance, "_images_to_delete"):
                        instance._images_to_delete = []
                    instance._images_to_delete.append(image)
                    logger.debug(
                        f"Marked ImageModel {getattr(image, 'id', 'unknown')} for deletion"
                    )

            except Exception as e:
                logger.exception(
                    f"Error in mark_orphaned_images_for_deletion for {sender.__name__} field {field.name}: {e}"
                )


@receiver(post_delete)
def delete_orphaned_images(sender, instance, **kwargs):
    # Skip if the sender is ImageModel itself
    if sender == ImageModel:
        return

    # Check if we have images marked for deletion
    if not hasattr(instance, "_images_to_delete"):
        return

    for image in instance._images_to_delete:
        try:
            # Double-check that no references exist
            is_referenced = False
            for rel in image._meta.related_objects:
                accessor_name = rel.get_accessor_name()
                if not accessor_name:
                    continue
                try:
                    related_manager = getattr(image, accessor_name)
                    if hasattr(related_manager, "exists") and related_manager.exists():
                        is_referenced = True
                        logger.debug(
                            f"ImageModel {getattr(image, 'id', 'unknown')} still has references via {accessor_name}"
                        )
                        break
                except Exception:
                    continue

            if not is_referenced:
                logger.info(
                    f"Deleting orphaned ImageModel {getattr(image, 'id', 'unknown')}"
                )
                # The post_delete signal on ImageModel will handle file cleanup
                with transaction.atomic():
                    image.delete()
            else:
                logger.debug(
                    f"ImageModel {getattr(image, 'id', 'unknown')} is still referenced, skipping deletion"
                )

        except Exception as e:
            logger.exception(
                f"Error deleting orphaned image {getattr(image, 'id', 'unknown')}: {e}"
            )
