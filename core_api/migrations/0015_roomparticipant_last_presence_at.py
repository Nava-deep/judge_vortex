from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core_api', '0014_examevent'),
    ]

    operations = [
        migrations.AddField(
            model_name='roomparticipant',
            name='last_presence_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
