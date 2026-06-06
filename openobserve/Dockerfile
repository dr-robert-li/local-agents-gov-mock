# The upstream OpenObserve image is distroless — no shell, curl, or wget — so a
# container-internal healthcheck is impossible out of the box. We layer in a
# single static (musl) busybox binary purely to provide `wget` for the compose
# healthcheck. Nothing else about the image changes.
FROM public.ecr.aws/zinclabs/openobserve:latest

COPY --from=busybox:musl /bin/busybox /bin/busybox
