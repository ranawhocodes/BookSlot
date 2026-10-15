"""add notification_logs table and IN_CONSULTATION booking status

Revision ID: a8f92d14e50b
Revises: 751c4826a31c
Create Date: 2026-10-15 14:50:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision = 'a8f92d14e50b'
down_revision = '751c4826a31c'
branch_labels = None
depends_on = None


NEW_BOOKING_STATUS_ENUM = sa.Enum(
    'PENDING_PAYMENT',
    'CONFIRMED',
    'IN_CONSULTATION',
    'COMPLETED',
    'CANCELLED',
    'NO_SHOW',
    'BLOCKED',
    'EXPIRED',
    name='booking_status',
    native_enum=False,
)

OLD_BOOKING_STATUS_ENUM = sa.Enum(
    'PENDING_PAYMENT',
    'CONFIRMED',
    'COMPLETED',
    'CANCELLED',
    'NO_SHOW',
    'BLOCKED',
    'EXPIRED',
    name='booking_status',
    native_enum=False,
)


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    existing_tables = inspector.get_table_names()

    # 1. Create notification_logs table if it does not exist yet
    if 'notification_logs' not in existing_tables:
        op.create_table(
            'notification_logs',
            sa.Column('id', sa.Uuid(), nullable=False),
            sa.Column('tenant_id', sa.Uuid(), nullable=False),
            sa.Column('booking_id', sa.Uuid(), nullable=True),
            sa.Column('customer_id', sa.Uuid(), nullable=True),
            sa.Column('recipient_phone', sa.String(length=20), nullable=False),
            sa.Column(
                'channel',
                sa.Enum('WHATSAPP', 'SMS', name='notification_channel', native_enum=False),
                nullable=False,
            ),
            sa.Column('provider', sa.String(length=50), nullable=False),
            sa.Column('template_name', sa.String(length=100), nullable=False),
            sa.Column(
                'status',
                sa.Enum('SENT', 'FAILED', 'QUOTA_EXCEEDED', name='notification_status', native_enum=False),
                nullable=False,
            ),
            sa.Column('payload', sa.JSON(), nullable=True),
            sa.Column(
                'created_at',
                sa.DateTime(timezone=True),
                server_default=sa.text('(CURRENT_TIMESTAMP)'),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(['booking_id'], ['bookings.id'], ondelete='SET NULL'),
            sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], ondelete='SET NULL'),
            sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('id'),
        )
        with op.batch_alter_table('notification_logs', schema=None) as batch_op:
            batch_op.create_index(
                batch_op.f('ix_notification_logs_tenant_id'),
                ['tenant_id'],
                unique=False,
            )

    # 2. Update bookings.status constraint to include IN_CONSULTATION
    if bind.dialect.name == 'postgresql':
        op.execute("ALTER TABLE bookings DROP CONSTRAINT IF EXISTS booking_status")
        op.execute(
            "ALTER TABLE bookings ADD CONSTRAINT booking_status "
            "CHECK (status IN ('PENDING_PAYMENT', 'CONFIRMED', 'IN_CONSULTATION', "
            "'COMPLETED', 'CANCELLED', 'NO_SHOW', 'BLOCKED', 'EXPIRED'))"
        )
    else:
        with op.batch_alter_table('bookings', schema=None) as batch_op:
            batch_op.alter_column(
                'status',
                existing_type=OLD_BOOKING_STATUS_ENUM,
                type_=NEW_BOOKING_STATUS_ENUM,
                existing_nullable=False,
            )


def downgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    existing_tables = inspector.get_table_names()

    if 'notification_logs' in existing_tables:
        with op.batch_alter_table('notification_logs', schema=None) as batch_op:
            batch_op.drop_index(batch_op.f('ix_notification_logs_tenant_id'))
        op.drop_table('notification_logs')

    if bind.dialect.name == 'postgresql':
        op.execute("ALTER TABLE bookings DROP CONSTRAINT IF EXISTS booking_status")
        op.execute(
            "ALTER TABLE bookings ADD CONSTRAINT booking_status "
            "CHECK (status IN ('PENDING_PAYMENT', 'CONFIRMED', "
            "'COMPLETED', 'CANCELLED', 'NO_SHOW', 'BLOCKED', 'EXPIRED'))"
        )
    else:
        with op.batch_alter_table('bookings', schema=None) as batch_op:
            batch_op.alter_column(
                'status',
                existing_type=NEW_BOOKING_STATUS_ENUM,
                type_=OLD_BOOKING_STATUS_ENUM,
                existing_nullable=False,
            )
