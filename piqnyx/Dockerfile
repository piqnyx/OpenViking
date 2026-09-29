# piqnyx: the running image with our files laid over it, and nothing else.
# SPDX-License-Identifier: AGPL-3.0
#
# Built by piqnyx/build.sh, which gives the three arguments from
# piqnyx/image.conf and first holds the list against the difference from the
# tag. What comes out is checked file by file by piqnyx/verify-image.sh.

ARG BASE
FROM ${BASE}

ARG SITE
ARG VERSION

# The checkout is lent to this one step at /mnt, a folder the image has and
# keeps empty, and is gone after it: nothing of the checkout stays in the image
# but what the step puts in place.
RUN --mount=type=bind,target=/mnt \
    sh /mnt/piqnyx/lay_over.sh /mnt "${SITE}" "${VERSION}" /app
