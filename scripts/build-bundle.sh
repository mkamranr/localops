#!/usr/bin/env bash
# Build the LocalOps offline (air-gapped) installation bundle.
#
# Run on an internet-connected Linux x86_64 build host with Docker (buildx) and git:
#   scripts/build-bundle.sh [--version 1.0.0] [--out dist] [--scan] [--allow-critical]
#
# Produces dist/localops-airgap-<version>-linux-amd64.tar.gz (+ .sha256) containing:
#   images/  config/  scripts/  models/  SBOM/  checksums/SHA256SUMS
#   manifest.json  README-OFFLINE.md
# See docs/airgap-installation.md.

# shellcheck source=scripts/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

SYFT_IMAGE="anchore/syft:v1.18.1"
TRIVY_IMAGE="aquasec/trivy:0.58.1"
PLATFORM="linux/amd64"

VERSION=""
OUT="$PROJECT_DIR/dist"
SCAN=0
ALLOW_CRITICAL=0
KEEP_STAGE=0

usage() { sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version) VERSION="$2"; shift 2 ;;
    --out) OUT="$2"; shift 2 ;;
    --scan) SCAN=1; shift ;;
    --allow-critical) ALLOW_CRITICAL=1; shift ;;
    --keep-stage) KEEP_STAGE=1; shift ;;
    -h | --help) usage 0 ;;
    *) warn "unknown option $1"; usage 1 ;;
  esac
done

require_cmd docker git tar gzip sha256sum awk sed
docker buildx version >/dev/null 2>&1 || die "docker buildx is required"
docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is required"

cd "$PROJECT_DIR"
if [[ -z "$VERSION" ]]; then
  VERSION="$(git describe --tags --always --dirty 2>/dev/null || echo dev)"
  VERSION="${VERSION#v}"
fi
[[ "$VERSION" =~ ^[A-Za-z0-9._-]+$ ]] || die "invalid version '$VERSION'"
REVISION="$(git rev-parse HEAD 2>/dev/null || echo unknown)"
BUILD_DATE="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
NAME="localops-airgap-${VERSION}-linux-amd64"
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"
STAGE="$OUT/$NAME"

avail_kb="$(df -Pk "$OUT" | awk 'NR==2 {print $4}')"
((avail_kb > 15 * 1024 * 1024)) || warn "less than 15 GiB free in $OUT; the build may run out of space"

rm -rf "$STAGE"
mkdir -p "$STAGE"/{images,config,scripts,models,SBOM,checksums}

info "LocalOps $VERSION ($REVISION) -> $STAGE"

# --------------------------------------------------------------- application images
info "Building application images for $PLATFORM"
APP_IMAGES=("localops-backend:$VERSION" "localops-frontend:$VERSION")
build_args=(--build-arg "LOCALOPS_VERSION=$VERSION" --build-arg "GIT_REVISION=$REVISION"
  --build-arg "BUILD_DATE=$BUILD_DATE")
docker buildx build --platform "$PLATFORM" --load --target runtime "${build_args[@]}" \
  -t "localops-backend:$VERSION" backend
docker buildx build --platform "$PLATFORM" --load "${build_args[@]}" \
  -t "localops-frontend:$VERSION" frontend

# --------------------------------------------------------------- third-party images
# The image list comes from the compose file itself, so it can never drift.
placeholder_env="$(mktemp)"
trap 'rm -f "$placeholder_env"' EXIT
sed 's/=CHANGE_ME$/=bundle-placeholder-0123456789abcdef0123456789/' .env.example >"$placeholder_env"
mapfile -t ALL_IMAGES < <(LOCALOPS_VERSION="$VERSION" docker compose --env-file "$placeholder_env" \
  -f docker-compose.yml config --images | sort -u)
THIRD_PARTY=()
for image in "${ALL_IMAGES[@]}"; do
  [[ "$image" == localops-* ]] || THIRD_PARTY+=("$image")
done
((${#THIRD_PARTY[@]} > 0)) || die "no third-party images found in docker-compose.yml"

info "Pulling third-party images: ${THIRD_PARTY[*]}"
for image in "${THIRD_PARTY[@]}"; do
  docker pull --platform "$PLATFORM" "$image" >/dev/null
done

# --------------------------------------------------------------- export + verify platform
info "Exporting images"
images_json=""
for image in "${APP_IMAGES[@]}" "${THIRD_PARTY[@]}"; do
  osarch="$(docker image inspect -f '{{.Os}}/{{.Architecture}}' "$image")"
  [[ "$osarch" == "$PLATFORM" ]] || die "$image is $osarch, expected $PLATFORM"
  id="$(docker image inspect -f '{{.Id}}' "$image")"
  digest="$(docker image inspect -f '{{join .RepoDigests ","}}' "$image")"
  file="images/$(printf '%s' "$image" | tr '/:' '__').tar.gz"
  role="third-party"
  [[ "$image" == localops-* ]] && role="application"
  docker save "$image" | gzip -6 >"$STAGE/$file"
  size="$(stat -c %s "$STAGE/$file")"
  printf '  %-55s %s\n' "$image" "$(du -h "$STAGE/$file" | cut -f1)"
  images_json+="$(printf '    {"ref": "%s", "role": "%s", "file": "%s", "image_id": "%s", "repo_digests": "%s", "size": %s},' \
    "$image" "$role" "$file" "$id" "$digest" "$size")"$'\n'
done
images_json="${images_json%,$'\n'}"

# --------------------------------------------------------------- SBOM + scan
info "Generating SBOMs (SPDX + CycloneDX) with $SYFT_IMAGE"
sock_gid="$(stat -c %g /var/run/docker.sock)"
for image in "${APP_IMAGES[@]}" "${THIRD_PARTY[@]}"; do
  base="$(printf '%s' "$image" | tr '/:' '__')"
  docker run --rm --user "$(id -u):$sock_gid" -e HOME=/tmp -e SYFT_CHECK_FOR_APP_UPDATE=false \
    -v /var/run/docker.sock:/var/run/docker.sock:ro \
    -v "$STAGE/SBOM:/out" "$SYFT_IMAGE" "docker:$image" \
    -o "spdx-json=/out/$base.spdx.json" -o "cyclonedx-json=/out/$base.cdx.json"
done

if [[ "$SCAN" == 1 ]]; then
  info "Scanning images for vulnerabilities with $TRIVY_IMAGE"
  mkdir -p "$STAGE/SBOM/vulnerabilities" "$OUT/.trivy-cache"
  critical=0
  for image in "${APP_IMAGES[@]}" "${THIRD_PARTY[@]}"; do
    base="$(printf '%s' "$image" | tr '/:' '__')"
    docker run --rm --user "$(id -u):$sock_gid" -e HOME=/tmp \
      -v /var/run/docker.sock:/var/run/docker.sock:ro \
      -v "$STAGE/SBOM/vulnerabilities:/out" -v "$OUT/.trivy-cache:/tmp/.cache" "$TRIVY_IMAGE" \
      image --quiet --format json --output "/out/$base.json" "$image"
    if ! docker run --rm --user "$(id -u):$sock_gid" -e HOME=/tmp \
      -v /var/run/docker.sock:/var/run/docker.sock:ro -v "$OUT/.trivy-cache:/tmp/.cache" \
      "$TRIVY_IMAGE" image --quiet --skip-db-update --severity CRITICAL --ignore-unfixed \
      --exit-code 3 --format table "$image"; then
      critical=1
    fi
  done
  if [[ "$critical" == 1 && "$ALLOW_CRITICAL" != 1 ]]; then
    die "CRITICAL vulnerabilities with available fixes found (see above); rebuild with updated base images or pass --allow-critical"
  fi
fi

# --------------------------------------------------------------- config, scripts, docs
info "Copying configuration, scripts and documentation"
cp docker-compose.yml docker-compose.bundle.yml docker-compose.tls.yml \
  docker-compose.secrets.yml docker-compose.airgap-test.yml .env.example "$STAGE/config/"
mkdir -p "$STAGE/config/deployment/tls"
cp -r deployment/nginx deployment/postgres "$STAGE/config/deployment/"
cp scripts/*.sh scripts/*.ps1 "$STAGE/scripts/"
chmod +x "$STAGE"/scripts/*.sh
cp deployment/models/* "$STAGE/models/"
cp docs/airgap-installation.md "$STAGE/README-OFFLINE.md"
cp docs/backup.md "$STAGE/BACKUP.md"
cp LICENSE "$STAGE/"

cat >"$STAGE/manifest.json" <<EOF
{
  "format": "localops-airgap-bundle",
  "format_version": 1,
  "name": "LocalOps",
  "version": "$VERSION",
  "git_revision": "$REVISION",
  "build_date": "$BUILD_DATE",
  "platform": "$PLATFORM",
  "requirements": {"docker": "24.0", "compose": "2.24", "disk_gb": 20, "memory_gb": 8},
  "images": [
$images_json
  ],
  "sbom_formats": ["spdx-json", "cyclonedx-json"],
  "vulnerability_scan": $([[ "$SCAN" == 1 ]] && echo true || echo false),
  "models_bundled": false
}
EOF

# --------------------------------------------------------------- checksums + archive
info "Writing checksums"
(cd "$STAGE" && find . -type f ! -path ./checksums/SHA256SUMS -printf '%P\n' | LC_ALL=C sort |
  xargs -d '\n' sha256sum >checksums/SHA256SUMS)

info "Creating archive"
tar -C "$OUT" -czf "$OUT/$NAME.tar.gz" "$NAME"
(cd "$OUT" && sha256sum "$NAME.tar.gz" >"$NAME.tar.gz.sha256")
[[ "$KEEP_STAGE" == 1 ]] || rm -rf "$STAGE"

info "Done: $OUT/$NAME.tar.gz ($(du -h "$OUT/$NAME.tar.gz" | cut -f1))"
echo "  sha256: $(cut -d' ' -f1 "$OUT/$NAME.tar.gz.sha256")"
echo "  Transfer both files to the air-gapped host, then:"
echo "    sha256sum -c $NAME.tar.gz.sha256 && tar xzf $NAME.tar.gz && cd $NAME"
echo "    sudo scripts/install.sh"
