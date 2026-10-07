import uuid
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.db.models.signals import post_delete
from django.dispatch import receiver
from django.utils import timezone


def _expiry():
    """Initial deadline. With the heartbeat on, a resume survives only RESUME_IDLE_SECONDS unless the open page keeps pinging."""
    idle = getattr(settings, "RESUME_IDLE_SECONDS", 0)
    ttl = settings.RESUME_TTL_SECONDS
    return timezone.now() + timedelta(seconds=min(idle, ttl) if idle else ttl)


class Resume(models.Model):
    """An uploaded resume. Everything about it (file, parsed data, results) is deleted at expires_at."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner_session = models.CharField(max_length=64, db_index=True)
    file = models.FileField(upload_to="resumes/")
    original_name = models.CharField(max_length=255)
    raw_text = models.TextField(blank=True)
    parsed = models.JSONField(default=dict)
    analysis = models.JSONField(default=dict, blank=True)   # job search results
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(default=_expiry, db_index=True)

    @property
    def hard_deadline(self):
        """Absolute maximum life of a resume (1 h by default), no matter how long the page stays open."""
        return self.created_at + timedelta(seconds=settings.RESUME_TTL_SECONDS)

    @property
    def seconds_left(self):
        return max(0, int((self.hard_deadline - timezone.now()).total_seconds()))

    def touch(self, seconds=None):
        """The owner is still here: push the idle deadline forward (never past the hard deadline)."""
        seconds = seconds or getattr(settings, "RESUME_IDLE_SECONDS", 0) or settings.RESUME_TTL_SECONDS
        new = min(timezone.now() + timedelta(seconds=seconds), self.hard_deadline)
        type(self).objects.filter(pk=self.pk).update(expires_at=new)
        self.expires_at = new

    def leaving(self):
        """Tab is closing / navigating away: shrink the deadline. The next page load of the owner touches it again."""
        new = min(timezone.now() + timedelta(seconds=getattr(settings, "RESUME_LEAVE_SECONDS", 45)), self.expires_at)
        type(self).objects.filter(pk=self.pk).update(expires_at=new)

    @property
    def expired(self):
        return self.expires_at <= timezone.now()


class GeneratedResume(models.Model):
    resume = models.ForeignKey(Resume, on_delete=models.CASCADE, related_name="generated")
    job_key = models.CharField(max_length=300)
    file = models.FileField(upload_to="generated/")
    created_at = models.DateTimeField(auto_now_add=True)


@receiver(post_delete, sender=Resume)
@receiver(post_delete, sender=GeneratedResume)
def _remove_file(sender, instance, **kwargs):
    """Delete the stored file from disk whenever its row is deleted."""
    if instance.file:
        instance.file.delete(save=False)


def purge_expired():
    """Delete every expired resume (rows + files + generated files). Returns how many were removed."""
    count = 0
    for r in Resume.objects.filter(expires_at__lte=timezone.now()):
        for g in r.generated.all():
            g.delete()
        r.delete()
        count += 1
    return count
