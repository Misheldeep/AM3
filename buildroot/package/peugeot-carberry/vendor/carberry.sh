#!/bin/sh
### BEGIN INIT INFO
# Provides:          carberry
# Required-Start:    $remote_fs $syslog
# Required-Stop:     $remote_fs $syslog
# Default-Start:     2 3 4 5
# Default-Stop:      0 1 6
# Short-Description: carberry initscript
# Description:       This file should be used to construct scripts to be placed in /etc/init.d.
### END INIT INFO
#
# Written by Massimo Savina <massimo.savina@paser.it>
#

set -e

DAEMON=/usr/local/bin/carberry/carberry_d/carberry
NAME=carberry

test -x $DAEMON || exit 0

case "$1" in
  start)
    echo -n "Starting server: $NAME "
    start-stop-daemon --start --background -m --pidfile /var/run/carberry.pid --exec $DAEMON
    echo "."
    ;;
  stop)
    echo -n "Stopping server: $NAME "
    start-stop-daemon --stop --pidfile /var/run/carberry.pid --oknodo --exec $DAEMON
    echo "."
      ;;
  restart)
    echo -n "Stopping server: $NAME "
    start-stop-daemon --stop --pidfile /var/run/carberry.pid --oknodo --exec $DAEMON
    echo "."
    
    wait
    echo -n "Starting server: $NAME "
    start-stop-daemon --start --background -m --pidfile /var/run/carberry.pid --exec $DAEMON
    echo "."
    ;;
  *)
    echo "Usage: /etc/init.d/$NAME {start|stop|restart}"
    exit 1
    ;;
esac

exit 0
