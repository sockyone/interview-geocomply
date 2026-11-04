"""Explicit database preparation; never run migrations during API startup."""
import argparse
import csv
import os

import psycopg
from psycopg.rows import dict_row


def connect():
    return psycopg.connect(os.environ['DATABASE_URL'], connect_timeout=3,
                           options='-c statement_timeout=5000 -c lock_timeout=3000',
                           row_factory=dict_row)


def read_products(path):
    """Validate the entire CSV before opening a database transaction."""
    products, seen = [], set()
    with open(path, newline='', encoding='utf-8') as source:
        reader = csv.DictReader(source)
        if reader.fieldnames != ['id', 'name', 'price_cents']:
            raise ValueError('Expected CSV columns: id,name,price_cents')
        for number, row in enumerate(reader, start=2):
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f'Invalid CSV row {number}')
            product_id, name = row['id'].strip(), row['name'].strip()
            price = row['price_cents']
            if not product_id or not name or product_id in seen:
                raise ValueError(f'Empty or duplicate product at row {number}')
            if not price.isascii() or not price.isdecimal() or not 0 < int(price) <= 2147483647:
                raise ValueError(f'Invalid price at row {number}')
            seen.add(product_id)
            products.append((product_id, name, int(price)))
    if not products:
        raise ValueError('Seed CSV must contain at least one product')
    return products


def prepare(seed=False, seed_file=None):
    if seed and seed_file is not None:
        raise ValueError('Choose --seed or --seed-file')
    products = read_products(seed_file) if seed_file is not None else (
        [('coffee', 'Coffee', 500), ('tea', 'Tea', 300)] if seed else [])
    with connect() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS products (
            id TEXT PRIMARY KEY, name TEXT NOT NULL,
            price_cents INTEGER NOT NULL CHECK (price_cents > 0))''')
        db.execute('''CREATE TABLE IF NOT EXISTS orders (
            id UUID PRIMARY KEY, product_id TEXT NOT NULL REFERENCES products(id),
            quantity INTEGER NOT NULL CHECK (quantity > 0),
            unit_price_cents INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','paid')),
            payment_id TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now())''')
        if products:
            with db.cursor() as cursor:
                cursor.executemany('''INSERT INTO products VALUES (%s, %s, %s)
                    ON CONFLICT (id) DO UPDATE
                    SET name=EXCLUDED.name, price_cents=EXCLUDED.price_cents''', products)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--seed', action='store_true')
    group.add_argument('--seed-file')
    arguments = parser.parse_args()
    prepare(seed=arguments.seed, seed_file=arguments.seed_file)
