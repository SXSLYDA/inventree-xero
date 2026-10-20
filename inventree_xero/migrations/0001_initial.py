from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name='XeroConnection',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('tenant_id', models.CharField(blank=True, max_length=64)),
                ('tenant_name', models.CharField(blank=True, max_length=200)),
                ('short_code', models.CharField(blank=True, max_length=20)),
                ('tenants', models.JSONField(blank=True, default=list)),
                ('access_token', models.TextField(blank=True)),
                ('refresh_token', models.TextField(blank=True)),
                ('expires_at', models.DateTimeField(blank=True, null=True)),
                ('scopes', models.TextField(blank=True)),
                ('connected_at', models.DateTimeField(blank=True, null=True)),
                ('connected_by', models.CharField(blank=True, max_length=150)),
                ('last_sync_started', models.DateTimeField(blank=True, null=True)),
                ('last_sync_finished', models.DateTimeField(blank=True, null=True)),
                ('last_sync_ok', models.BooleanField(blank=True, null=True)),
                ('last_sync_message', models.TextField(blank=True)),
                ('import_marks', models.JSONField(blank=True, default=dict)),
            ],
            options={
                'verbose_name': 'Xero connection',
                'verbose_name_plural': 'Xero connection',
            },
        ),
        migrations.CreateModel(
            name='XeroSyncLog',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created', models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ('level', models.CharField(choices=[('info', 'Info'), ('warning', 'Warning'), ('error', 'Error')], default='info', max_length=10)),
                ('direction', models.CharField(choices=[('export', 'InvenTree → Xero'), ('import', 'Xero → InvenTree'), ('system', 'System')], default='system', max_length=10)),
                ('doc_type', models.CharField(blank=True, help_text='po, so, contact, item, system', max_length=20)),
                ('action', models.CharField(blank=True, max_length=20)),
                ('inventree_ref', models.CharField(blank=True, max_length=100)),
                ('inventree_pk', models.IntegerField(blank=True, null=True)),
                ('xero_id', models.CharField(blank=True, max_length=64)),
                ('xero_number', models.CharField(blank=True, max_length=100)),
                ('message', models.TextField(blank=True)),
            ],
            options={
                'verbose_name': 'Xero sync log entry',
                'verbose_name_plural': 'Xero sync log',
                'ordering': ['-created'],
            },
        ),
    ]
