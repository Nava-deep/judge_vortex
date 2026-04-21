from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core_api', '0015_roomparticipant_last_presence_at'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='ExamWorkspaceSnapshot',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('language', models.CharField(default='python', max_length=50)),
                ('code', models.TextField(blank=True, default='')),
                ('files', models.JSONField(blank=True, default=list)),
                ('entry_file', models.CharField(blank=True, default='', max_length=255)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('question', models.ForeignKey(on_delete=models.deletion.CASCADE, related_name='workspace_snapshots', to='core_api.examquestion')),
                ('room', models.ForeignKey(on_delete=models.deletion.CASCADE, related_name='workspace_snapshots', to='core_api.examroom')),
                ('student', models.ForeignKey(on_delete=models.deletion.CASCADE, related_name='exam_workspace_snapshots', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'unique_together': {('room', 'student', 'question')},
            },
        ),
    ]
