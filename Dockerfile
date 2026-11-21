FROM python:3.13.13-slim@sha256:aa938a849bcb82dce8f49480f056ab82bf5c1c3ebc294f0430f37b6820e7f286
WORKDIR /bot
COPY mechfight.py arena.py combat.py bot.py ./
ENTRYPOINT ["python3", "-B", "-u", "/bot/bot.py"]
