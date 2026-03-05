:: rebuild-all.cmd
@echo off
echo Rebuilding builder image...
docker compose --profile build-image build --no-cache builder-image
echo Compiling project...
docker compose run --rm compiler
echo Done
