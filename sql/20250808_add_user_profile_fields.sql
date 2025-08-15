-- Add profile fields to users
alter table if exists users
  add column if not exists name text,
  add column if not exists birthday date,
  add column if not exists profession text,
  add column if not exists business_name text,
  add column if not exists business_address text;
