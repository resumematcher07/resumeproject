from django.contrib import admin

from .models import GeneratedResume, Resume


@admin.register(Resume)
class ResumeAdmin(admin.ModelAdmin):
    list_display = ("id", "original_name", "created_at", "expires_at")
    exclude = ("raw_text",)


admin.site.register(GeneratedResume)
