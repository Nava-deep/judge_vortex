from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core_api', '0010_submission_passed_testcases_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='roomparticipant',
            name='access_locked',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='roomparticipant',
            name='access_locked_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
