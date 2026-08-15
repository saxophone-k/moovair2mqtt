/* Minimal SysV message-queue tool for the Moovair thermostat (ARM, static).
 * Built for local-control recon: learn the rac_queue envelope, then inject.
 *
 *   msgtool peek <msqid>          -> non-blocking msgrcv ONE msg, hexdump (DESTRUCTIVE:
 *                                    only use on the dead msqid 6, never the live channel)
 *   msgtool send <msqid> <hex>    -> msgsnd a message built from a hex string
 *                                    (first 8 hex bytes little-endian = mtype long,
 *                                     rest = mtext). Prints result.
 *
 * Intentionally does NOT loop / drain. One op per invocation.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <sys/ipc.h>
#include <sys/msg.h>
#include <time.h>

#define MAXT 4096
struct mbuf { long mtype; char mtext[MAXT]; };

static int hexbyte(const char *p){int hi,lo;char c;
  c=p[0]; hi=(c>='0'&&c<='9')?c-'0':(c|32)-'a'+10;
  c=p[1]; lo=(c>='0'&&c<='9')?c-'0':(c|32)-'a'+10;
  return (hi<<4)|lo;}

int main(int argc, char **argv){
  if(argc<3){fprintf(stderr,"usage: %s peek|send <msqid> [hex]\n",argv[0]);return 2;}
  int msqid=atoi(argv[2]);

  if(!strcmp(argv[1],"peek")){
    struct mbuf m; memset(&m,0,sizeof m);
    ssize_t n=msgrcv(msqid,&m,MAXT,0,IPC_NOWAIT);
    if(n<0){fprintf(stderr,"msgrcv err: %s\n",strerror(errno));return 1;}
    printf("mtype=%ld len=%zd\n",m.mtype,n);
    for(ssize_t i=0;i<n;i++){printf("%02x ",(unsigned char)m.mtext[i]); if((i&15)==15)printf("\n");}
    printf("\n");
    return 0;
  }
  if(!strcmp(argv[1],"send")){
    if(argc<4){fprintf(stderr,"need hex\n");return 2;}
    const char *h=argv[3]; int nb=strlen(h)/2;
    unsigned char *raw=malloc(nb); for(int i=0;i<nb;i++) raw[i]=hexbyte(h+2*i);
    if(nb<4){fprintf(stderr,"need >=4 bytes (mtype)\n");return 2;}
    struct mbuf m; memset(&m,0,sizeof m);
    /* first 4 bytes LE -> mtype (long is 4B on arm32); rest -> mtext */
    m.mtype = raw[0]|(raw[1]<<8)|(raw[2]<<16)|((long)raw[3]<<24);
    int payload=nb-4; if(payload>MAXT)payload=MAXT;
    memcpy(m.mtext, raw+4, payload);
    if(msgsnd(msqid,&m,payload,IPC_NOWAIT)<0){fprintf(stderr,"msgsnd err: %s\n",strerror(errno));return 1;}
    printf("sent mtype=%ld payload=%d to msqid=%d\n",m.mtype,payload,msqid);
    return 0;
  }
  if(!strcmp(argv[1],"grab")){
    /* Spin msgrcv(IPC_NOWAIT); catch EVERY message over the window, re-send
     * each immediately so dev_app still gets it, print event(mtext[4:8]) +
     * first 96 bytes. Safe capture of the live command channel. */
    int secs = argc>=4?atoi(argv[3]):20;
    time_t t0=time(0); int cnt=0;
    struct mbuf m;
    while(time(0)-t0 < secs){
      memset(&m,0,sizeof m);
      ssize_t n=msgrcv(msqid,&m,MAXT,0,IPC_NOWAIT);
      if(n>=0){
        msgsnd(msqid,&m,n,0);              /* put it straight back */
        unsigned ev = (unsigned char)m.mtext[4]|((unsigned char)m.mtext[5]<<8)
                    |((unsigned char)m.mtext[6]<<16)|((unsigned char)m.mtext[7]<<24);
        int show = n<96?n:96;
        printf("[%d] event=%u len=%zd : ",++cnt,ev,n);
        for(int i=0;i<show;i++) printf("%02x ",(unsigned char)m.mtext[i]);
        printf("\n");
      }
    }
    printf("captured %d msgs in %ds\n",cnt,secs);
    return 0;
  }
  fprintf(stderr,"unknown op\n");return 2;
}
