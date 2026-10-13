-- Сетевые маршруты: прокси и порядок их использования для каждого внешнего сервиса.
CREATE TABLE net_proxies (
    id bigserial PRIMARY KEY,
    name text NOT NULL UNIQUE CHECK (name ~ '^[A-Za-z0-9][A-Za-z0-9 ._-]{0,39}$'),
    scheme text NOT NULL CHECK (scheme IN ('http', 'https', 'socks5', 'socks5h')),
    host text NOT NULL CHECK (length(host) BETWEEN 1 AND 200),        -- для показа; адрес целиком (с логином) хранится зашифрованным
    url_enc text NOT NULL,
    is_enabled boolean NOT NULL DEFAULT true,
    last_test_at timestamptz,
    last_test_ok boolean,
    last_test_error text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE net_policies (
    service text PRIMARY KEY CHECK (service ~ '^[a-z][a-z0-9_]{1,29}$'),
    strategy text NOT NULL DEFAULT 'failover' CHECK (strategy IN ('failover', 'auto', 'round_robin')),
    attempts integer NOT NULL DEFAULT 3 CHECK (attempts BETWEEN 1 AND 10),
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- Упорядоченные маршруты сервиса; proxy_id NULL — прямой доступ.
CREATE TABLE net_routes (
    id bigserial PRIMARY KEY,
    service text NOT NULL REFERENCES net_policies(service) ON DELETE CASCADE,
    proxy_id bigint REFERENCES net_proxies(id) ON DELETE CASCADE,
    position integer NOT NULL DEFAULT 0,
    is_enabled boolean NOT NULL DEFAULT true
);
CREATE UNIQUE INDEX ux_net_routes_proxy ON net_routes(service, proxy_id) WHERE proxy_id IS NOT NULL;
CREATE UNIQUE INDEX ux_net_routes_direct ON net_routes(service) WHERE proxy_id IS NULL;

-- Снимок здоровья маршрутов (обновляется службами раз в 30 секунд; нужен для панели).
CREATE TABLE net_health (
    service text NOT NULL,
    route text NOT NULL,
    process text NOT NULL,
    successes bigint NOT NULL DEFAULT 0,
    failures bigint NOT NULL DEFAULT 0,
    consecutive integer NOT NULL DEFAULT 0,
    latency_ms double precision,
    open_seconds integer NOT NULL DEFAULT 0,
    last_error text NOT NULL DEFAULT '',
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (service, route, process)
);
