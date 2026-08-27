/*------------------------------------------------------------------------------
  Carberry Project
  www.carberry.it
  Massimo Savina
------------------------------------------------------------------------------*/
#include <sys/types.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <sys/ipc.h>
#include <sys/shm.h>
#include <unistd.h>
#include <stdlib.h>
#include <stdarg.h>
#include <stdio.h>
#include <time.h>
#include <string.h>
#include <fcntl.h>
#include <errno.h>
#include <termios.h>
#include <malloc.h>
#include <semaphore.h>
#include <signal.h>

#include "carberry.h"

TSockData socks[FD_SETSIZE];

#define ERASEUP  "\033[2K\033[1A"
#define PORTNAME_OLD "/dev/ttyAMA0"
#define PORTNAME_NEW "/dev/ttyS0"
#define BAUDRATE B115200

char portname[32] = PORTNAME_NEW;
int tcpport = 7070;
int comport;
int mainsock;

fd_set activesocks;
fd_set toreadsocks;
fd_set gpiosocks;

/*------------------------------------------------------------------------------
------------------------------------------------------------------------------*/
unsigned int BoardDetect(void)
{
  FILE *fp;
  char path[1035];

  /* Open the command for reading. */
  fp = popen("cat /proc/cpuinfo | grep Revision | awk '{ print $3 }'", "r");
  if (fp == NULL) 
  {
    printf("Failed to run command\r\n" );
  }

  /* Read the output a line at a time - output it. */
  while (fgets(path, sizeof(path)-1, fp) != NULL) 
  {
    if (
	strstr(path, "0002") 
	|| strstr(path, "0003") 
	|| strstr(path, "0004") 
	|| strstr(path, "0005") 
	|| strstr(path, "0006") 
	|| strstr(path, "0007") 
	|| strstr(path, "0008") 
	|| strstr(path, "0009") 
	|| strstr(path, "000d")
	|| strstr(path, "000e") 
	|| strstr(path, "000f") 
	|| strstr(path, "0010")
	|| strstr(path, "0013") 
	|| strstr(path, "900032")
	|| strstr(path, "0012") 
	|| strstr(path, "0015")
	|| strstr(path, "a01041")
	|| strstr(path, "a21041") 
	|| strstr(path, "a22042")
	)
    {
      strcpy(portname, PORTNAME_OLD);
      printf("UART PORT will be -> %s\r\n", portname);
      return 0;
    }    
  }
  /* close */
  pclose(fp);
  
  printf("UART PORT will be -> %s\r\n", portname);
  return 0;
}

/*------------------------------------------------------------------------------
------------------------------------------------------------------------------*/
unsigned int OpenUARTPort(void)
{
	struct termios newtio;

	comport = open(portname, O_RDWR | O_NOCTTY);

  if (comport < 0)
  {
    printf("Unable to open %s\r\n", portname);
    return false;
  }

	bzero(&newtio, sizeof(newtio));

	newtio.c_cflag = BAUDRATE | CS8 | CLOCAL | CREAD;
  newtio.c_iflag = IGNPAR | IGNBRK;
  newtio.c_lflag = 0;
	newtio.c_oflag = 0;

  newtio.c_cc[VMIN]  = 0;
  newtio.c_cc[VTIME] = 1;

  tcflush(comport, TCIOFLUSH);
  tcsetattr(comport, TCSANOW, &newtio);
  fcntl(comport, F_SETFL, 0);

  return true;
}

/*------------------------------------------------------------------------------
------------------------------------------------------------------------------*/
void CloseUARTPort()
{
  if (comport)
  {
    shutdown(comport, SHUT_RDWR);
    close(comport);
  }
}

/*------------------------------------------------------------------------------
------------------------------------------------------------------------------*/
int setsocket(int sock)
{
  int i;

  for (i=0; i<FD_SETSIZE; i++)
  {
    //Free slot
    if (!socks[i].socket)
    {
      //Store socket identifier
      socks[i].socket = sock;
      socks[i].caret = 0;
      return 0;
    }
  }
  printf("Unable to add socket\r\n");
  return 1;
}

/*------------------------------------------------------------------------------
------------------------------------------------------------------------------*/
int clrsocket(int sock)
{
  int i;

  for (i=0; i<FD_SETSIZE; i++)
  {
    if (socks[i].socket == sock)
    {
      socks[i].socket = 0;
      return 0;
    }
  }
  printf("Socket not found\r\n");
  return 1;
}

/*------------------------------------------------------------------------------
------------------------------------------------------------------------------*/
void ApplicationTerminate()
{
  int i;
  int socket;

  for (i=0; i<FD_SETSIZE; i++)
  {
    socket = socks[i].socket;
    if (socket > 0)
    {
      shutdown(socket, SHUT_RDWR);
      close(socket);
    }
  }
  shutdown(mainsock, SHUT_RDWR);
  close(mainsock);
  CloseUARTPort();
  printf("Application Terminated\r\n");
}

/*------------------------------------------------------------------------------
------------------------------------------------------------------------------*/
void terminal_kill_handler(int signum)
{
  printf("\r\nTerminal kill signal handled\r\n");
  ApplicationTerminate();
  exit(0);
}

/*------------------------------------------------------------------------------
------------------------------------------------------------------------------*/
void SendUART(int port, const char* format, ...)
{
  char message[STDSTR];
  va_list ap;

	va_start(ap, format);
	vsprintf(message, format, ap);
	va_end(ap);

  //Send to Carberry
  write(port, message, strlen(message));
}

/*------------------------------------------------------------------------------
------------------------------------------------------------------------------*/
int SendSOCK(int filedes, const char* format, ...)
{
  char message[STDSTR];
  va_list ap;

	va_start(ap, format);
	vsprintf(message, format, ap);
	va_end(ap);

  //Debug
  printf(message);

  if (filedes)
  {
    //Send to socket
    int sent = send(filedes, message, strlen(message), 0);
    if (sent <= 0)
    {
      //Maybe socket has closed
      close(filedes);
      shutdown(filedes, SHUT_RDWR);
      clrsocket(filedes);
      FD_CLR(filedes, &activesocks);
      printf("Socket %i disconnected\r\n", filedes);
      return false;
    }
  }
  return true;
}

/*------------------------------------------------------------------------------
------------------------------------------------------------------------------*/
void ProcCommandLine(char* inpline, int filedes, int index)
{
  //Debug
  //printf("Sock %d Rx -> %s", index, inpline);

  char ch = 0;
  int i = 0;
  int timeout = 0;
  char txbuf[STDSTR];
  int chunked = false;

  //Send to Carberry
  SendUART(comport, inpline);

  while (timeout < 50)
  {
    //If we see CR...
    if (ch == '\n')
    {
      //Evaluate end of command
      txbuf[i] = 0;
      if (strstr(txbuf, "OK"))    break;
      if (strstr(txbuf, "ERROR")) break;
    }

    if (read(comport, &ch, 1) == 1)
    {
      timeout = 0;
      txbuf[i++] = ch;
      if (i == (STDSTR-1))
      {
        txbuf[i] = 0;
        send(filedes, txbuf, strlen(txbuf), 0);
        if (!chunked)
        {
          //Debug
          //printf("Sock %d Tx -> %s", index, txbuf);
        }
        else
        {
          //Debug
          //printf("%s", txbuf);
        }
        chunked = true;
        i = 0;
      }
    }
    else
    {
      timeout++;
    }
  }

  if (timeout < 50)
  {
    txbuf[i] = 0;
    send(filedes, txbuf, strlen(txbuf), 0);
    if (!chunked)
    {
      //Debug
      //printf("Sock %d Tx -> %s\r\n", index, txbuf);
    }
    else
    {
      //Debug
      //printf("%s\r\n", txbuf);
    }
  }
  else
  {
    //Timeout
    SendSOCK(filedes, "ERROR - UART Timeout!\r\n");
  }
}

/*------------------------------------------------------------------------------
------------------------------------------------------------------------------*/
int EvaluateSocket(int filedes, int index)
{
  char rxbuf[STDSTR];
  char scanchr;
  int i;
  int nbytes;

  //Get socket chars
  nbytes = recv(filedes, rxbuf, STDSTR, 0);
  //Error
  if (nbytes <  0) return  1;
  //End of communication
  if (nbytes == 0) return -1;

  for (i=0; i<nbytes; i++)
  {
    scanchr = rxbuf[i];

    switch (scanchr)
    {
      //Carriage return
      case '\n':
      break;

      //Line Feed
      case '\r':
      {
        if (socks[index].caret)
        {
          //Process Command
          socks[index].strbuf[socks[index].caret++] = '\r';
          socks[index].strbuf[socks[index].caret++] = '\n';
          socks[index].strbuf[socks[index].caret]   = '\0';
          ProcCommandLine(socks[index].strbuf, filedes, socks[index].socket);
        }
        //Reset pointer for the next call
        socks[index].caret = 0;
      }
      break;

      //Other chars
      default:
        if (socks[index].caret < STDSTR-2)
        {
          socks[index].strbuf[socks[index].caret++] = scanchr;
        }
      break;
    }
  }
  return 0;
}



/*------------------------------------------------------------------------------
------------------------------------------------------------------------------*/
void ProcessSelectTimeout()
{
  static unsigned int count = 0;

  if (++count > 10)
  {
    count = 0;
  }
}


/*------------------------------------------------------------------------------
------------------------------------------------------------------------------*/
void DispatchUARTEvents()
{
  int i;
  char rxbuf[STDSTR];
  int nbytes;

  //Get data from UART
  nbytes = read(comport, rxbuf, STDSTR);
  if (nbytes == 0) return;

  for (i=0; i<FD_SETSIZE; i++)
  {
    int filedes = socks[i].socket;
    if (filedes)
    {
      int sent = send(filedes, rxbuf, nbytes, 0);
      if (sent <= 0)
      {
        //Chiudiamo il socket
        close(filedes);
        shutdown(filedes, SHUT_RDWR);
        clrsocket(filedes);
        FD_CLR(filedes, &activesocks);
        printf("Socket %i disconnected\r\n", filedes);
      }
    }
  }
}

/*------------------------------------------------------------------------------
------------------------------------------------------------------------------*/
int main(int argc, char* argv[])
{
  int i;
  int newsock;
  int currsock;
  struct sockaddr_in srvaddress;
  struct sockaddr_in clnaddress;
  size_t size;
  struct timeval timeout;

  printf("Carberry Started\r\n");
  BoardDetect();

  //Kill hook
  if (signal(SIGINT,  terminal_kill_handler) == SIG_IGN) signal(SIGINT,  SIG_IGN);
  if (signal(SIGTERM, terminal_kill_handler) == SIG_IGN) signal(SIGTERM, SIG_IGN);

  //Try to open UART port
  if (!OpenUARTPort())
  {
    printf("Unable to open UART port\r\n");
    return 1;
  }

  //Try to open main socket
  if ((mainsock = socket(PF_INET, SOCK_STREAM, 0)) < 0)
  {
    CloseUARTPort();
    printf("Unable to open main socket\r\n");
    return 1;
  }

  //Componiamo l'indirizzo del server
  srvaddress.sin_port        = htons(tcpport);
  srvaddress.sin_family      = AF_INET;
  srvaddress.sin_addr.s_addr = INADDR_ANY;

  //Connettiamo il socket
  if (bind(mainsock, (struct sockaddr*)&srvaddress, sizeof(srvaddress)) < 0)
  {
    printf("Socket Bind Error\r\n");
    //Terminiamo subito
    return 1;
  }

  //Ci mettiamo in ascolto
  if (listen (mainsock, 1) < 0)
  {
    printf("Listen Error\r\n");
    //Terminiamo subito
    return 1;
  }
  
  //Inizializziamo il contenitore dei sockets
  FD_ZERO(&activesocks);
  //Aggiungiamo alla lista dei socket quello del main
  FD_SET(mainsock, &activesocks);
  //Aggiungiamo la UART
  FD_SET(comport, &activesocks);

  while (true)
  {
    //Leggiamo tra i socks attivi
    memmove(&toreadsocks, &activesocks, sizeof(toreadsocks));

    //Impostiamo il timeout
    timeout.tv_sec  = 1;
    timeout.tv_usec = 0;

    //Analizziamo tra i socks attivi se qualcuno � da leggere
    switch (select(FD_SETSIZE, &toreadsocks, NULL, NULL, &timeout))
    {
      //Errore
      case -1:
        printf("Select Error! Sleeping a bit and retry\r\n");
        sleep(1);
      break;

      //Timeout
      case 0:
        ProcessSelectTimeout();
      break;

      //Data
      default:
        //Ci sono dati sul socket principale?
        if (FD_ISSET(mainsock, &toreadsocks))
        {
          //Si tratta della richiesta di una nuova connessione
          size = sizeof(clnaddress);
          //Accettiamo la nuova richiesta
          newsock = accept(mainsock, (struct sockaddr*)&clnaddress, &size);
          //Verifica errori...
          if (newsock < 0)
          {
            printf("Accept Error! Sleeping a bit and retry\r\n");
            sleep(1);
          }
          else
          {
            //Un po' di log
            printf("Host %s connected on socket #%d\r\n", inet_ntoa(clnaddress.sin_addr), newsock);
            //Memorizziamo il nuovo socket
            setsocket(newsock);
            //Aggiungiamo alla lista la nuova connessione
            FD_SET(newsock, &activesocks);
          }
        }

        //Ci sono dati sulla UART?
        if (FD_ISSET(comport, &toreadsocks))
        {
          DispatchUARTEvents();
        }

        //Scanniamo e serviamo tutte le altre richieste pendenti
        for (i=0; i<FD_SETSIZE; i++)
        {
          //Estraiamo uno ad uno i socket memorizzati
          currsock = socks[i].socket;
          if (currsock > 0)
          {
            //Ci sono dati sulle connessioni aggiunte?
            if (FD_ISSET(currsock, &toreadsocks))
            {
              //Il client si � diconnesso
              if (EvaluateSocket(currsock, i) < 0)
              {
                //Chiudiamo il socket
                close(currsock);
                shutdown(currsock, SHUT_RDWR);
                //Distruggiamo l'istanza della classe terminale relativa alla connessione
                clrsocket(currsock);
                //Lo togliamo dalla lista dei socket attivi
                FD_CLR(currsock, &activesocks);
                //Un po' di log
                printf("Socket #%d disconnected\r\n", currsock);
              }
            }
          }
        }
      break;
    }
  }

  ApplicationTerminate();
  return 0;
}


