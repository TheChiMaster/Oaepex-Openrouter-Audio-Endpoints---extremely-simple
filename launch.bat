@REM  Launches both front and backend for you
@echo off

cd backend && start uv run uvicorn server:app
cd..\Oapex-front-end && start npm run dev