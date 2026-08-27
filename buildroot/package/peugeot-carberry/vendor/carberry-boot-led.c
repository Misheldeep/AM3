#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <termios.h>
#include <unistd.h>

#define PORTNAME "/dev/ttyAMA0"
#define BAUDRATE B115200

static int write_all(int fd, const void *buf, size_t len)
{
    const unsigned char *p = buf;

    while (len > 0) {
        ssize_t n = write(fd, p, len);

        if (n < 0) {
            if (errno == EINTR)
                continue;

            perror("write");
            return -1;
        }

        p += n;
        len -= (size_t)n;
    }

    return 0;
}

int main(void)
{
    static const char command[] = "GPLED LED2 SET\r";

    struct termios tio;
    int fd;

    fd = open(PORTNAME, O_RDWR | O_NOCTTY | O_CLOEXEC);

    if (fd < 0) {
        perror("carberry-boot-led: open " PORTNAME);
        return 1;
    }

    memset(&tio, 0, sizeof(tio));

    /* Deliberately mirrors official carberry_d 1.5. */
    tio.c_cflag = BAUDRATE | CS8 | CLOCAL | CREAD;
    tio.c_iflag = IGNPAR | IGNBRK;
    tio.c_lflag = 0;
    tio.c_oflag = 0;

    tio.c_cc[VMIN]  = 0;
    tio.c_cc[VTIME] = 1;

    if (tcflush(fd, TCIOFLUSH) < 0) {
        perror("carberry-boot-led: tcflush");
        close(fd);
        return 1;
    }

    if (tcsetattr(fd, TCSANOW, &tio) < 0) {
        perror("carberry-boot-led: tcsetattr");
        close(fd);
        return 1;
    }

    if (write_all(fd, command, sizeof(command) - 1) < 0) {
        close(fd);
        return 1;
    }

    /*
     * Ensure the bytes have physically left the UART before close().
     * We deliberately do NOT wait for or parse the PIC reply.
     */
    if (tcdrain(fd) < 0) {
        perror("carberry-boot-led: tcdrain");
        close(fd);
        return 1;
    }

    close(fd);
    return 0;
}
