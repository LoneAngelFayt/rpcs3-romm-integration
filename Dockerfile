FROM scratch

COPY root/ /

RUN apt-get update \
 && apt-get install -y python3 xdotool unzip p7zip-full \
 && rm -rf /var/lib/apt/lists/*
