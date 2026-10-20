from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [('inventree_xero', '0002_xeroquotelink')]

    operations = [
        migrations.CreateModel(
            name='XeroWriteAudit',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created', models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ('mode', models.CharField(choices=[('live', 'Live'), ('dry_run', 'Dry run')], default='live', max_length=10)),
                ('method', models.CharField(max_length=10)),
                ('path', models.CharField(max_length=200)),
                ('doc_type', models.CharField(blank=True, max_length=20)),
                ('inventree_ref', models.CharField(blank=True, db_index=True, max_length=100)),
                ('inventree_pk', models.IntegerField(blank=True, null=True)),
                ('idempotency_key', models.CharField(blank=True, max_length=128)),
                ('request_body', models.JSONField(blank=True, default=dict)),
                ('response_status', models.IntegerField(blank=True, null=True)),
                ('response_body', models.JSONField(blank=True, default=dict)),
                ('ok', models.BooleanField(default=False)),
                ('error', models.TextField(blank=True)),
            ],
            options={
                'verbose_name': 'Xero write (audit)',
                'verbose_name_plural': 'Xero writes (audit)',
                'ordering': ['-created'],
            },
        ),
    ]
