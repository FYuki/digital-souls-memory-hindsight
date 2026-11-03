FROM groonga/pgroonga:4.1.0-debian-18@sha256:817261cb4ed9dc8270d46af5395f40710782d58769823022a704396b80d07dc7

USER root
# Debian は snapshot.debian.org の固定時点、PGDG は全版を残す apt-archive から入れる。
# 最新版を自動で使わない。時点は公開から 7 日以上たったものを選び、意識的に上げる
# （手順は README の「DB イメージのパッケージ固定」）。上げたら packages.lock を作り直す。
ARG DEBIAN_SNAPSHOT=20261027T000000Z
RUN mkdir -p /etc/apt/sources.list.d.disabled \
    && mv /etc/apt/sources.list.d/* /etc/apt/sources.list.d.disabled/ \
    && printf '%s\n' \
       'Types: deb' \
       "URIs: https://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}" \
       'Suites: trixie trixie-updates' \
       'Components: main' \
       'Signed-By: /usr/share/keyrings/debian-archive-keyring.pgp' \
       '' \
       'Types: deb' \
       "URIs: https://snapshot.debian.org/archive/debian-security/${DEBIAN_SNAPSHOT}" \
       'Suites: trixie-security' \
       'Components: main' \
       'Signed-By: /usr/share/keyrings/debian-archive-keyring.pgp' \
       > /etc/apt/sources.list.d/debian-snapshot.sources \
    && echo 'deb [ signed-by=/usr/local/share/keyrings/postgres.gpg.asc ] https://apt-archive.postgresql.org/pub/repos/apt trixie-pgdg-archive main' \
       > /etc/apt/sources.list.d/pgdg-archive.list \
    && apt-get -o Acquire::Check-Valid-Until=false update \
    && apt-get install -y --no-install-recommends \
       build-essential=12.12 postgresql-server-dev-18=18.6-1.pgdg13+2 libpq-dev=18.6-1.pgdg13+2 libicu-dev=76.1-4 \
    && rm -rf /var/lib/apt/lists/* /etc/apt/sources.list.d/* \
    && mv /etc/apt/sources.list.d.disabled/* /etc/apt/sources.list.d/ \
    && rmdir /etc/apt/sources.list.d.disabled

ADD --checksum=sha256:69f4019389af05dc1c9548deb8628e62878e6e207c03907f2b8af2016472cdaa https://codeload.github.com/pgvector/pgvector/tar.gz/refs/tags/v0.8.2 /build/vector.tar.gz
ADD --checksum=sha256:d67e2acbbd89985ed8b3e22c00d6f65a275c1487f4ab10b132d9b29a2a6d2616 https://codeload.github.com/pgbigm/pg_bigm/tar.gz/735dceba0ecdd8ac1aaaaa207226a7102b6bbd71 /build/bigm.tar.gz
RUN tar -xzf /build/vector.tar.gz -C /build \
    && tar -xzf /build/bigm.tar.gz -C /build \
    && make -C /build/pgvector-0.8.2 OPTFLAGS="" with_llvm=no \
    && make -C /build/pgvector-0.8.2 install with_llvm=no \
    && make -C /build/pg_bigm-735dceba0ecdd8ac1aaaaa207226a7102b6bbd71 USE_PGXS=1 with_llvm=no \
    && make -C /build/pg_bigm-735dceba0ecdd8ac1aaaaa207226a7102b6bbd71 install USE_PGXS=1 with_llvm=no \
    && dpkg-query -W > /usr/share/retrieval-build-packages.txt \
    && rm -rf /build

COPY packages.lock /usr/share/retrieval-expected-packages.txt
RUN cmp /usr/share/retrieval-build-packages.txt /usr/share/retrieval-expected-packages.txt
