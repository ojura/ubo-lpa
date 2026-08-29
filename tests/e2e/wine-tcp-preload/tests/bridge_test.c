#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <poll.h>
#include <signal.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <unistd.h>

static void fail(const char *what)
{
    perror(what);
    exit(1);
}

static void send_fd(int socket_fd, int fd, char payload)
{
    char control[CMSG_SPACE(sizeof(fd))];
    struct cmsghdr *cmsg;
    struct iovec iov = { .iov_base = &payload, .iov_len = 1 };
    struct msghdr msg;

    memset(&msg, 0, sizeof(msg));
    memset(control, 0, sizeof(control));
    msg.msg_iov = &iov;
    msg.msg_iovlen = 1;
    msg.msg_control = control;
    msg.msg_controllen = sizeof(control);
    cmsg = CMSG_FIRSTHDR(&msg);
    cmsg->cmsg_level = SOL_SOCKET;
    cmsg->cmsg_type = SCM_RIGHTS;
    cmsg->cmsg_len = CMSG_LEN(sizeof(fd));
    memcpy(CMSG_DATA(cmsg), &fd, sizeof(fd));
    msg.msg_controllen = cmsg->cmsg_len;
    if (sendmsg(socket_fd, &msg, 0) != 1) fail("sendmsg");
}

static int receive_fd(int socket_fd, char expected_payload, pid_t expected_credential)
{
    char control[256];
    char payload = 0;
    struct iovec iov = { .iov_base = &payload, .iov_len = 1 };
    struct msghdr msg;
    struct cmsghdr *cmsg;
    int received_fd = -1;
    pid_t credential = -1;

    memset(&msg, 0, sizeof(msg));
    memset(control, 0, sizeof(control));
    msg.msg_iov = &iov;
    msg.msg_iovlen = 1;
    msg.msg_control = control;
    msg.msg_controllen = sizeof(control);
    if (recvmsg(socket_fd, &msg, MSG_CMSG_CLOEXEC) != 1) fail("recvmsg");
    if (payload != expected_payload)
    {
        errno = EPROTO;
        fail("payload mismatch");
    }
    for (cmsg = CMSG_FIRSTHDR(&msg); cmsg; cmsg = CMSG_NXTHDR(&msg, cmsg))
    {
        if (cmsg->cmsg_level != SOL_SOCKET) continue;
        if (cmsg->cmsg_type == SCM_RIGHTS)
            memcpy(&received_fd, CMSG_DATA(cmsg), sizeof(received_fd));
#ifdef SCM_CREDENTIALS
        else if (cmsg->cmsg_type == SCM_CREDENTIALS)
        {
            const struct ucred *credentials = (const struct ucred *)CMSG_DATA(cmsg);
            credential = credentials->pid;
        }
#endif
    }
    if (received_fd < 0)
    {
        errno = ENOMSG;
        fail("missing SCM_RIGHTS");
    }
    if (expected_credential > 0 && credential != expected_credential)
    {
        errno = EACCES;
        fail("missing or wrong SCM_CREDENTIALS");
    }
    return received_fd;
}

static void wait_readable(int fd)
{
    struct pollfd pfd = { .fd = fd, .events = POLLIN };
    int ret;

    do ret = poll(&pfd, 1, 5000); while (ret < 0 && errno == EINTR);
    if (ret != 1 || !(pfd.revents & POLLIN))
    {
        errno = ETIMEDOUT;
        fail("poll translated connection");
    }
}

static void require_readable_now(int fd, const char *what)
{
    struct pollfd pfd = { .fd = fd, .events = POLLIN };
    int ret;

    do ret = poll(&pfd, 1, 0); while (ret < 0 && errno == EINTR);
    if (ret != 1 || !(pfd.revents & POLLIN))
    {
        errno = ETIMEDOUT;
        fail(what);
    }
}

static void require_not_readable_now(int fd, const char *what)
{
    struct pollfd pfd = { .fd = fd, .events = POLLIN };
    int ret;

    do ret = poll(&pfd, 1, 0); while (ret < 0 && errno == EINTR);
    if (ret != 0)
    {
        errno = EPROTO;
        fail(what);
    }
}

static void require_unix_endpoint(int fd, const char *what)
{
    int domain = 0;
    socklen_t size = sizeof(domain);

    if (getsockopt(fd, SOL_SOCKET, SO_DOMAIN, &domain, &size) < 0 ||
        size != sizeof(domain) || domain != AF_UNIX)
    {
        errno = EPROTOTYPE;
        fail(what);
    }
}

static void send_payload(int fd, char payload)
{
    struct iovec iov = { .iov_base = &payload, .iov_len = 1 };
    struct msghdr msg;

    memset(&msg, 0, sizeof(msg));
    msg.msg_iov = &iov;
    msg.msg_iovlen = 1;
    if (sendmsg(fd, &msg, 0) != 1) fail("send payload");
}

static void receive_payload(int fd, char expected, int flags)
{
    char payload = 0;
    struct iovec iov = { .iov_base = &payload, .iov_len = 1 };
    struct msghdr msg;

    memset(&msg, 0, sizeof(msg));
    msg.msg_iov = &iov;
    msg.msg_iovlen = 1;
    if (recvmsg(fd, &msg, flags) != 1) fail("recv payload");
    if (payload != expected)
    {
        errno = EPROTO;
        fail("peek payload mismatch");
    }
}

static int exercise_dup_chain(int fd)
{
    int first;
    int second;
    int third;

    first = dup(fd);
    if (first < 0) fail("dup translated fd");
    close(fd);

    second = open("/dev/null", O_RDONLY | O_CLOEXEC);
    if (second < 0) fail("open dup2 target");
    if (dup2(first, second) != second) fail("dup2 translated fd");
    close(first);

    third = open("/dev/null", O_RDONLY | O_CLOEXEC);
    if (third < 0) fail("open dup3 target");
    if (dup3(second, third, O_CLOEXEC) != third) fail("dup3 translated fd");
    close(second);
    return third;
}

/* Wine starts wineserver as a child of its first client.  On systems using
 * Yama's default ptrace policy, that child may copy an FD from its parent only
 * after the parent explicitly authorizes it. */
static void exercise_child_server(const char *socket_path)
{
    struct sockaddr_un address;
    int ready[2];
    int listener;
    int connected;
    int accepted;
    int status;
    pid_t child;
    char byte;
    socklen_t address_len;

    memset(&address, 0, sizeof(address));
    address.sun_family = AF_UNIX;
    if (strlen(socket_path) >= sizeof(address.sun_path)) fail("child-server path");
    strcpy(address.sun_path, socket_path);
    address_len = (socklen_t)(offsetof(struct sockaddr_un, sun_path) +
                              strlen(socket_path) + 1);
    if (pipe(ready) < 0) fail("child-server ready pipe");
    child = fork();
    if (child < 0) fail("fork child server");
    if (!child)
    {
        close(ready[0]);
        listener = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
        if (listener < 0) fail("child-server socket");
        if (bind(listener, (struct sockaddr *)&address, address_len) < 0)
            fail("child-server bind");
        if (listen(listener, 1) < 0) fail("child-server listen");
        if (write(ready[1], "R", 1) != 1) fail("child-server ready write");
        close(ready[1]);
        accepted = accept(listener, NULL, NULL);
        if (accepted < 0) fail("child-server accept");
        require_unix_endpoint(accepted, "child-server endpoint is not AF_UNIX");
        if (read(accepted, &byte, 1) != 1 || byte != 'Q')
            fail("child-server request");
        if (write(accepted, "A", 1) != 1) fail("child-server response");
        close(accepted);
        close(listener);
        _exit(0);
    }

    close(ready[1]);
    if (read(ready[0], &byte, 1) != 1 || byte != 'R') fail("child-server ready read");
    close(ready[0]);
    connected = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (connected < 0) fail("parent-client socket");
    if (connect(connected, (struct sockaddr *)&address, address_len) < 0)
        fail("parent-client connect");
    require_unix_endpoint(connected, "parent-client endpoint is not AF_UNIX");
    if (write(connected, "Q", 1) != 1) fail("parent-client request");
    if (read(connected, &byte, 1) != 1 || byte != 'A') fail("parent-client response");
    close(connected);
    if (waitpid(child, &status, 0) != child) fail("wait child server");
    if (!WIFEXITED(status) || WEXITSTATUS(status))
    {
        errno = ECHILD;
        fail("child-server status");
    }
    if (unlink(socket_path) < 0) fail("unlink child-server marker");
}

int main(void)
{
    char directory[] = "bridge-test-XXXXXX";
    char socket_path[sizeof(((struct sockaddr_un *)0)->sun_path)];
    char unrelated_path[sizeof(socket_path)];
    char unrelated_sidecar[sizeof(socket_path) + 16];
    struct sockaddr_un address;
    struct sockaddr_in internal_address;
    struct stat st;
    int listener;
    int connected;
    int accepted;
    int to_child[2];
    int from_child[2];
    int peek_release[2];
    int received;
    int enable = 1;
    int native_pair[2];
    int competing_listener;
    int status;
    int flags;
    socklen_t internal_address_len = sizeof(internal_address);
    pid_t child;
    pid_t attacker;
    char buffer[32] = {0};

    signal(SIGPIPE, SIG_IGN);
    if (socketpair(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0, native_pair) < 0)
        fail("native socketpair");
    close(native_pair[0]);
    close(native_pair[1]);
    if (!mkdtemp(directory)) fail("mkdtemp");
    if (snprintf(socket_path, sizeof(socket_path), "%s/socket", directory) >=
        (int)sizeof(socket_path)) fail("socket path");
    if (snprintf(unrelated_path, sizeof(unrelated_path), "%s/unrelated", directory) >=
        (int)sizeof(unrelated_path) ||
        snprintf(unrelated_sidecar, sizeof(unrelated_sidecar), "%s.winetcp", unrelated_path) >=
        (int)sizeof(unrelated_sidecar))
        fail("unrelated paths");
    received = open(unrelated_path, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0600);
    if (received < 0) fail("create unrelated file");
    close(received);
    if (mkfifo(unrelated_sidecar, 0600) < 0) fail("create unrelated FIFO");
    if (unlink(unrelated_path) < 0) fail("unlink unrelated file");
    if (lstat(unrelated_sidecar, &st) < 0 || !S_ISFIFO(st.st_mode))
        fail("unrelated FIFO preserved");
    if (unlink(unrelated_sidecar) < 0) fail("unlink unrelated FIFO");

    listener = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (listener < 0) fail("socket listener");
    memset(&address, 0, sizeof(address));
    address.sun_family = AF_UNIX;
    strcpy(address.sun_path, socket_path);
    if (bind(listener, (struct sockaddr *)&address,
             (socklen_t)(offsetof(struct sockaddr_un, sun_path) + strlen(socket_path) + 1)) < 0)
        fail("bind listener");
    if (listen(listener, 4) < 0) fail("listen");
    if (lstat(socket_path, &st) < 0 || !S_ISSOCK(st.st_mode)) fail("socket marker");
    if ((st.st_mode & 0777) != 0600)
    {
        errno = EACCES;
        fail("socket marker mode");
    }
    competing_listener = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (competing_listener < 0) fail("competing socket");
    errno = 0;
    if (bind(competing_listener, (struct sockaddr *)&address,
             (socklen_t)(offsetof(struct sockaddr_un, sun_path) + strlen(socket_path) + 1)) != -1 ||
        errno != EADDRINUSE)
    {
        errno = EPROTO;
        fail("competing bind reservation");
    }
    close(competing_listener);

    memset(&internal_address, 0, sizeof(internal_address));
    if (getsockname(listener, (struct sockaddr *)&internal_address, &internal_address_len) < 0)
        fail("internal getsockname");
    attacker = fork();
    if (attacker < 0) fail("fork attacker");
    if (!attacker)
    {
        char partial = 'X';
        int raw = socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, 0);
        if (raw < 0) _exit(20);
        if (connect(raw, (struct sockaddr *)&internal_address, sizeof(internal_address)) < 0)
            _exit(21);
        if (send(raw, &partial, 1, MSG_NOSIGNAL) != 1) _exit(22);
        pause();
        _exit(23);
    }
    errno = 0;
    if (accept(listener, NULL, NULL) != -1 || errno != ETIMEDOUT)
    {
        errno = EPROTO;
        fail("partial handshake timeout");
    }
    kill(attacker, SIGTERM);
    if (waitpid(attacker, &status, 0) != attacker) fail("wait attacker");

    if (pipe(peek_release) < 0) fail("peek release pipe");
    child = fork();
    if (child < 0) fail("fork");
    if (!child)
    {
        int child_received;
        char response[] = "child-to-parent";

        close(listener);
        close(peek_release[1]);
        connected = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
        if (connected < 0) fail("child socket");
        if (setsockopt(connected, SOL_SOCKET, SO_PASSCRED, &enable, sizeof(enable)) < 0)
            fail("child SO_PASSCRED");
        if (connect(connected, (struct sockaddr *)&address,
                    (socklen_t)(offsetof(struct sockaddr_un, sun_path) + strlen(socket_path) + 1)) < 0)
            fail("child connect");
        require_unix_endpoint(connected, "child endpoint is not AF_UNIX");
        child_received = fcntl(connected, F_DUPFD_CLOEXEC, 64);
        if (child_received < 0) fail("child F_DUPFD_CLOEXEC");
        close(connected);
        connected = child_received;

        wait_readable(connected);
        child_received = receive_fd(connected, 'A', getppid());
        if (read(child_received, buffer, sizeof(buffer)) != 15 ||
            memcmp(buffer, "parent-to-child", 15))
        {
            errno = EIO;
            fail("child transferred pipe");
        }
        if (read(child_received, buffer, 1) != 0)
        {
            errno = EIO;
            fail("child pipe EOF");
        }
        close(child_received);

        send_payload(connected, 'P');
        if (read(peek_release[0], buffer, 1) != 1)
            fail("peek release read");
        close(peek_release[0]);

        if (pipe(from_child) < 0) fail("child pipe");
        send_fd(connected, from_child[0], 'B');
        close(from_child[0]);
        if (write(from_child[1], response, sizeof(response) - 1) != (ssize_t)(sizeof(response) - 1))
            fail("child write");
        close(from_child[1]);
        close(connected);
        _exit(0);
    }

    accepted = accept(listener, NULL, NULL);
    if (accepted < 0) fail("accept");
    require_unix_endpoint(accepted, "accepted endpoint is not AF_UNIX");
    close(peek_release[0]);
    accepted = exercise_dup_chain(accepted);
    flags = fcntl(accepted, F_GETFD);
    if (flags < 0 || fcntl(accepted, F_SETFD, flags | FD_CLOEXEC) < 0 ||
        !(fcntl(accepted, F_GETFD) & FD_CLOEXEC))
        fail("translated F_SETFD");
    flags = fcntl(accepted, F_GETFL);
    if (flags < 0 || fcntl(accepted, F_SETFL, flags | O_NONBLOCK) < 0)
        fail("translated F_SETFL");
    if (pipe(to_child) < 0) fail("parent pipe");
    send_fd(accepted, to_child[0], 'A');
    close(to_child[0]);
    if (write(to_child[1], "parent-to-child", 15) != 15) fail("parent write");
    close(to_child[1]);

    wait_readable(accepted);
    receive_payload(accepted, 'P', MSG_PEEK);
    require_readable_now(accepted, "readiness after first MSG_PEEK");
    receive_payload(accepted, 'P', MSG_PEEK);
    require_readable_now(accepted, "readiness after second MSG_PEEK");
    receive_payload(accepted, 'P', 0);
    require_not_readable_now(accepted, "readiness after consuming peeked payload");
    if (write(peek_release[1], "x", 1) != 1) fail("peek release write");
    close(peek_release[1]);

    wait_readable(accepted);
    received = receive_fd(accepted, 'B', -1);
    if (read(received, buffer, sizeof(buffer)) != 15 ||
        memcmp(buffer, "child-to-parent", 15))
    {
        errno = EIO;
        fail("parent transferred pipe");
    }
    if (read(received, buffer, 1) != 0)
    {
        errno = EIO;
        fail("parent pipe EOF");
    }
    close(received);
    close(accepted);
    close(listener);
    if (waitpid(child, &status, 0) != child) fail("waitpid");
    if (!WIFEXITED(status) || WEXITSTATUS(status))
    {
        errno = ECHILD;
        fail("child status");
    }

    if (unlink(socket_path) < 0) fail("unlink marker");
    if (snprintf(socket_path, sizeof(socket_path), "%s/server-child", directory) >=
        (int)sizeof(socket_path)) fail("child-server socket path");
    exercise_child_server(socket_path);
    if (rmdir(directory) < 0) fail("rmdir");
    puts("bridge test: PASS");
    return 0;
}
