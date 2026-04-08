from django.conf import settings
from django.db import models


class Campaign(models.Model):
    """A marketing campaign for a specific product."""

    PRODUCT_CHOICES = [
        ("boyshort", "Boyshort"),
        ("shapewear", "Shapewear"),
        ("briefs", "Briefs"),
    ]

    name = models.CharField(max_length=255)
    product = models.CharField(max_length=50, choices=PRODUCT_CHOICES, blank=True)
    description = models.TextField(blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="campaigns",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.name

    @property
    def generators_count(self):
        """Count of generators in this campaign."""
        return self.generators.count()

    @property
    def ads_count(self):
        """Count of ads in this campaign (across all generators)."""
        # Import here to avoid circular reference since Ad is defined below
        from django.db.models import Count
        result = self.generators.aggregate(total=Count("ads"))
        return result["total"] or 0


class CustomerPersona(models.Model):
    """A customer persona for targeting ads."""

    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="personas",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Asset(models.Model):
    """An image asset for use in ad generation."""

    TYPE_STYLE = "style"
    TYPE_PRODUCT = "product"
    TYPE_CHOICES = [
        (TYPE_STYLE, "Style Reference"),
        (TYPE_PRODUCT, "Product Reference"),
    ]

    name = models.CharField(max_length=255, blank=True)
    asset_type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    image = models.ImageField(upload_to="assets/")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="assets",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class StyleReference(Asset):
    """Proxy of Asset filtered to style references — for Django admin grouping."""

    class Meta:
        proxy = True
        verbose_name = "Style Reference"
        verbose_name_plural = "Style References"


class ProductReference(Asset):
    """Proxy of Asset filtered to product references — for Django admin grouping."""

    class Meta:
        proxy = True
        verbose_name = "Product Reference"
        verbose_name_plural = "Product References"


class Generator(models.Model):
    """A generator within a campaign."""

    STATUS_PENDING = "pending"
    STATUS_PROCESSING = "processing"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_PROCESSING, "Processing"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_FAILED, "Failed"),
    ]

    campaign = models.ForeignKey(
        Campaign,
        on_delete=models.CASCADE,
        related_name="generators",
    )
    title = models.CharField(max_length=255)
    brief = models.TextField(blank=True)
    headlines = models.TextField(blank=True, help_text="Headlines for the ads, one per line")
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING,
    )
    customer_persona = models.ForeignKey(
        CustomerPersona,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="generators",
    )
    style_references = models.ManyToManyField(
        Asset,
        blank=True,
        related_name="generators_as_style",
        limit_choices_to={"asset_type": Asset.TYPE_STYLE},
    )
    product_references = models.ManyToManyField(
        Asset,
        blank=True,
        related_name="generators_as_product",
        limit_choices_to={"asset_type": Asset.TYPE_PRODUCT},
    )
    number_of_ads = models.PositiveIntegerField(default=5)
    dimensions = models.CharField(max_length=50, blank=True)
    placement = models.CharField(max_length=50, blank=True)
    is_template = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.title

    @property
    def ads_count(self):
        """Count of ads in this generator."""
        return self.ads.count()

    @property
    def approved_count(self):
        """Count of approved ads in this generator."""
        return self.ads.filter(status=Ad.STATUS_APPROVED).count()


class Ad(models.Model):
    """A generated ad within a generator."""

    STATUS_PENDING = "pending"
    STATUS_APPROVED = "approved"
    STATUS_REJECTED = "rejected"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_APPROVED, "Approved"),
        (STATUS_REJECTED, "Rejected"),
    ]

    generator = models.ForeignKey(
        Generator,
        on_delete=models.CASCADE,
        related_name="ads",
    )
    headline = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    image = models.ImageField(upload_to="ads/", blank=True)
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING,
    )
    generation_metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text="Stores generation context (prompt, references) for revisions",
    )

    # Critique results from ImageCritic
    critique_score = models.FloatField(
        null=True,
        blank=True,
        help_text="Overall quality score (1-10) from ImageCritic",
    )
    critique_passed = models.BooleanField(
        default=True,
        help_text="Whether image passed all 7 quality checks",
    )
    critique_summary = models.TextField(
        blank=True,
        help_text="Summary of critique result",
    )
    critique_data = models.JSONField(
        default=dict,
        blank=True,
        help_text="Full CritiqueResult data for analysis",
    )
    was_auto_revised = models.BooleanField(
        default=False,
        help_text="Whether this image was auto-revised by critique system",
    )
    revision_count = models.PositiveIntegerField(
        default=0,
        help_text="Number of auto-revisions performed",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.headline


class APISettings(models.Model):
    """Singleton model for API configuration."""

    PROVIDER_CHOICES = [
        ("gemini", "Gemini (Primary) → OpenAI (Fallback)"),
        ("openai", "OpenAI (Primary) → Gemini (Fallback)"),
        ("gemini_only", "Gemini Only"),
        ("openai_only", "OpenAI Only"),
    ]

    GEMINI_MODEL_CHOICES = [
        ("gemini-2.5-flash-image", "Gemini 2.5 Flash Image (Fast)"),
        ("gemini-3.1-flash-image-preview", "Gemini 3.1 Flash Image Preview (High Volume)"),
        ("gemini-3-pro-image-preview", "Gemini 3 Pro Image Preview (Professional)"),
    ]

    CRITIC_MODEL_CHOICES = [
        ("gemini-2.5-flash", "Gemini 2.5 Flash (Fast)"),
        ("gemini-2.5-pro", "Gemini 2.5 Pro (Higher quality)"),
    ]

    primary_provider = models.CharField(
        max_length=20,
        choices=PROVIDER_CHOICES,
        default="gemini",
        help_text="Which AI provider to use for image generation",
    )
    gemini_api_key = models.CharField(max_length=255, blank=True)
    openai_api_key = models.CharField(max_length=255, blank=True)
    gemini_model = models.CharField(
        max_length=100,
        choices=GEMINI_MODEL_CHOICES,
        default="gemini-2.5-flash-image",
        help_text="Gemini model to use for image generation",
    )
    critic_model = models.CharField(
        max_length=100,
        choices=CRITIC_MODEL_CHOICES,
        default="gemini-2.5-flash",
        help_text="Model for image quality critique",
    )
    critic_max_retries = models.PositiveSmallIntegerField(
        default=3,
        help_text="Max retries for critic API rate limits (0-10)",
    )
    master_prompt = models.TextField(
        blank=True,
        default="",
        help_text="Master prompt instructions for image generation",
    )
    updated_at = models.DateTimeField(auto_now=True)

    DEFAULT_MASTER_PROMPT = """BRAND: Everdries leakproof underwear for women 65+. Tone: warm, relatable, empowering — never clinical or patronizing. No Trustpilot references. No 'premium' messaging. Do NOT include any logo or brand mark in the image.

BACKGROUND INTEGRATION: Blend the ad background seamlessly with the model photo's existing background. Avoid jarring color contrasts.

TEXT AMOUNT: Less text often performs better. You do NOT need every feature listed above. Only include copy that fits naturally."""

    class Meta:
        verbose_name = "API Settings"
        verbose_name_plural = "API Settings"

    def save(self, *args, **kwargs):
        self.pk = 1  # Ensure singleton
        super().save(*args, **kwargs)

    @classmethod
    def get_settings(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class AdMessage(models.Model):
    """Chat message for ad edit requests."""

    ROLE_USER = "user"
    ROLE_ASSISTANT = "assistant"
    ROLE_CHOICES = [
        (ROLE_USER, "User"),
        (ROLE_ASSISTANT, "Assistant"),
    ]

    ad = models.ForeignKey(
        Ad,
        on_delete=models.CASCADE,
        related_name="messages",
    )
    role = models.CharField(max_length=20, choices=ROLE_CHOICES)
    content = models.TextField()
    is_error = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.role}: {self.content[:50]}"
