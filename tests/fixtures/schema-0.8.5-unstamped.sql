-- The schema release 0.8.5 built on an empty PostgreSQL 17: create_all from its
-- models, no alembic_version table (the project had no migrations yet).
-- pg_dump --schema-only --no-owner --no-privileges, comments removed.
-- Read by tests/test_wiring.py (InitDbAgainstPostgresTests).

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

CREATE TYPE public.downloadstatus AS ENUM (
    'PENDING',
    'DOWNLOADING',
    'COMPLETED',
    'ERROR'
);

CREATE TYPE public.itemstatus AS ENUM (
    'PENDING_PARSE',
    'PARSED',
    'CHECKING',
    'CHECKED',
    'PENDING_REVIEW',
    'APPROVED',
    'REJECTED',
    'SEARCHING',
    'DOWNLOADING',
    'COMPLETED',
    'NO_MATCH',
    'PARSE_FAILED',
    'ERROR'
);

CREATE TYPE public.mediatype AS ENUM (
    'MOVIE',
    'SERIES',
    'AUDIOBOOK',
    'EBOOK',
    'MAGAZINE',
    'COMIC',
    'MUSIC',
    'UNKNOWN'
);

CREATE TYPE public.recommendation AS ENUM (
    'APPROVE',
    'REJECT',
    'NEUTRAL'
);

CREATE TYPE public.severityrating AS ENUM (
    'NONE',
    'MILD',
    'MODERATE',
    'SEVERE'
);

SET default_tablespace = '';

SET default_table_access_method = heap;

CREATE TABLE public.content_checks (
    id integer NOT NULL,
    media_item_id integer NOT NULL,
    tmdb_id integer,
    tmdb_title character varying(500),
    imdb_id character varying(20),
    poster_url character varying(500),
    backdrop_url character varying(500),
    genres jsonb,
    keywords jsonb,
    plot text,
    certifications jsonb,
    sex_rating public.severityrating,
    violence_rating public.severityrating,
    profanity_rating public.severityrating,
    frightening_rating public.severityrating,
    has_magic boolean NOT NULL,
    magic_indicators jsonb,
    checked_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE SEQUENCE public.content_checks_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.content_checks_id_seq OWNED BY public.content_checks.id;

CREATE TABLE public.downloads (
    id integer NOT NULL,
    media_item_id integer NOT NULL,
    torrent_search_id integer,
    qbittorrent_hash character varying(100),
    magnet_url text,
    download_url text,
    category character varying(50),
    save_path character varying(500),
    progress double precision NOT NULL,
    status public.downloadstatus NOT NULL,
    started_at timestamp with time zone,
    completed_at timestamp with time zone
);

CREATE SEQUENCE public.downloads_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.downloads_id_seq OWNED BY public.downloads.id;

CREATE TABLE public.media_items (
    id integer NOT NULL,
    telegram_message_id bigint NOT NULL,
    telegram_chat_id bigint NOT NULL,
    raw_text text NOT NULL,
    media_type public.mediatype NOT NULL,
    title character varying(500),
    year integer,
    quality character varying(50),
    source character varying(50),
    size_text character varying(100),
    season character varying(20),
    release_group character varying(100),
    external_links jsonb,
    telegram_photo_path character varying(200),
    status public.itemstatus NOT NULL,
    auto_recommendation public.recommendation NOT NULL,
    error_message text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    reviewed_at timestamp with time zone
);

CREATE SEQUENCE public.media_items_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.media_items_id_seq OWNED BY public.media_items.id;

CREATE TABLE public.torrent_searches (
    id integer NOT NULL,
    media_item_id integer NOT NULL,
    prowlarr_results jsonb,
    result_count integer NOT NULL,
    best_match_guid character varying(500),
    best_match_title character varying(500),
    best_match_size bigint,
    best_match_seeders integer,
    best_match_url character varying(1000),
    searched_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE SEQUENCE public.torrent_searches_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.torrent_searches_id_seq OWNED BY public.torrent_searches.id;

ALTER TABLE ONLY public.content_checks ALTER COLUMN id SET DEFAULT nextval('public.content_checks_id_seq'::regclass);

ALTER TABLE ONLY public.downloads ALTER COLUMN id SET DEFAULT nextval('public.downloads_id_seq'::regclass);

ALTER TABLE ONLY public.media_items ALTER COLUMN id SET DEFAULT nextval('public.media_items_id_seq'::regclass);

ALTER TABLE ONLY public.torrent_searches ALTER COLUMN id SET DEFAULT nextval('public.torrent_searches_id_seq'::regclass);

ALTER TABLE ONLY public.content_checks
    ADD CONSTRAINT content_checks_media_item_id_key UNIQUE (media_item_id);

ALTER TABLE ONLY public.content_checks
    ADD CONSTRAINT content_checks_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.downloads
    ADD CONSTRAINT downloads_media_item_id_key UNIQUE (media_item_id);

ALTER TABLE ONLY public.downloads
    ADD CONSTRAINT downloads_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.media_items
    ADD CONSTRAINT media_items_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.torrent_searches
    ADD CONSTRAINT torrent_searches_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.media_items
    ADD CONSTRAINT uq_message_chat UNIQUE (telegram_message_id, telegram_chat_id);

CREATE INDEX ix_media_items_media_type ON public.media_items USING btree (media_type);

CREATE INDEX ix_media_items_status ON public.media_items USING btree (status);

ALTER TABLE ONLY public.content_checks
    ADD CONSTRAINT content_checks_media_item_id_fkey FOREIGN KEY (media_item_id) REFERENCES public.media_items(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.downloads
    ADD CONSTRAINT downloads_media_item_id_fkey FOREIGN KEY (media_item_id) REFERENCES public.media_items(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.downloads
    ADD CONSTRAINT downloads_torrent_search_id_fkey FOREIGN KEY (torrent_search_id) REFERENCES public.torrent_searches(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.torrent_searches
    ADD CONSTRAINT torrent_searches_media_item_id_fkey FOREIGN KEY (media_item_id) REFERENCES public.media_items(id) ON DELETE CASCADE;
