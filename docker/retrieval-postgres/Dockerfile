FROM groonga/pgroonga:4.1.0-debian-18@sha256:817261cb4ed9dc8270d46af5395f40710782d58769823022a704396b80d07dc7

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       build-essential=12.12 postgresql-server-dev-18=18.6-1.pgdg13+2 libicu-dev=76.1-4 \
    && rm -rf /var/lib/apt/lists/*

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
