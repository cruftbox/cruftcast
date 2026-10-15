#!/bin/sh
# Two-note bell (E6 then C6), each with two decaying overtones.
N1='(0.32*sin(2*PI*1318.5*t)*exp(-3.5*t)+0.10*sin(2*PI*2637*t)*exp(-7*t)+0.05*sin(2*PI*3955.5*t)*exp(-10*t))'
D='(t-0.32)'
N2="gt(t\,0.32)*(0.32*sin(2*PI*1046.5*$D)*exp(-3*$D)+0.10*sin(2*PI*2093*$D)*exp(-6*$D)+0.05*sin(2*PI*3139.5*$D)*exp(-9*$D))"
ffmpeg -y -loglevel error -f lavfi -i "aevalsrc=$N1+$N2:s=24000:d=1.8" \
  -af "afade=t=in:d=0.005,afade=t=out:st=1.4:d=0.4" \
  -ac 1 -c:a libmp3lame -b:a 48k "${1:-assets/chime.mp3}"
