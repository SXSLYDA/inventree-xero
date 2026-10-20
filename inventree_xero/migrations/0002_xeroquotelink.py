from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [('inventree_xero', '0001_initial')]

    operations = [
        migrations.CreateModel(
            name='XeroQuoteLink',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('quote_request_id', models.PositiveIntegerField(unique=True)),
                ('reference', models.CharField(blank=True, db_index=True, max_length=32)),
                ('xero_id', models.CharField(blank=True, db_index=True, max_length=64)),
                ('number', models.CharField(blank=True, max_length=50)),
                ('status', models.CharField(blank=True, help_text='Xero quote status', max_length=20)),
                ('shop_status', models.CharField(blank=True, help_text='Shop quote status when last synced', max_length=20)),
                ('url', models.CharField(blank=True, max_length=500)),
                ('hash', models.CharField(blank=True, help_text='What was last sent to Xero', max_length=40)),
                ('shop_sig', models.CharField(blank=True, help_text='Shop quote lines when last synced', max_length=40)),
                ('xero_sig', models.CharField(blank=True, help_text='Xero quote lines when last synced', max_length=40)),
                ('total', models.CharField(blank=True, max_length=40)),
                ('updated', models.DateTimeField(blank=True, help_text='Xero UpdatedDateUTC when last synced', null=True)),
                ('synced', models.DateTimeField(blank=True, null=True)),
                ('sales_order_id', models.PositiveIntegerField(blank=True, null=True)),
                ('invoiced', models.BooleanField(default=False)),
                ('final', models.BooleanField(default=False)),
                ('error', models.TextField(blank=True)),
                ('created', models.DateTimeField(default=django.utils.timezone.now)),
            ],
            options={
                'verbose_name': 'Xero quote link',
                'verbose_name_plural': 'Xero quote links',
                'ordering': ['-created'],
            },
        ),
    ]
