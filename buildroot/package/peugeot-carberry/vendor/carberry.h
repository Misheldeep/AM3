/*------------------------------------------------------------------------------
	carberry daemon changelog

	rel. 1.00 	01/05/2014 Massimo Savina
	- Initial release

------------------------------------------------------------------------------*/
#include <time.h>
#include <stdio.h>
#include <sys/time.h>

#define false 0
#define true  1

#define STDSTR                1024

typedef struct
{
  int  caret;
  char strbuf[STDSTR];
  int  socket;
} TSockData;

