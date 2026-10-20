with source as (
    select * from {{ source('raw_whatsapp', 'expenses') }}
),

renamed as (
    select
        transaction_id,
        message_id,
        user_phone_hash as user_id,
        input_type,
        cast(transaction_date as date) as transaction_date,
        coalesce(transaction_time, '00:00:00'::time) as transaction_time,
        cast(amount as numeric(12,2)) as amount_original,
        upper(trim(currency)) as currency_code,
        upper(trim(category)) as category_name,
        coalesce(nullif(trim(subcategory), ''), 'Otros') as subcategory_name,
        coalesce(nullif(trim(merchant_name), ''), 'Sin Identificar') as merchant_name,
        upper(trim(payment_method)) as payment_method_code,
        description,
        confidence_score,
        created_at as ingested_at_utc
    from source
)

select * from renamed
