@echo off
docker compose --profile build-image build builder-image
echo Builder image created/updated