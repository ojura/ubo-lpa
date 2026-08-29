#define _GNU_SOURCE

#include <arpa/inet.h>
#include <dlfcn.h>
#include <endian.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <pthread.h>
#include <stddef.h>
#include <stdarg.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <sys/random.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <sys/un.h>
#include <unistd.h>

/*
 * Wine TCP master-socket shim
 * ---------------------------
 *
 * Some sandboxes reject socket(AF_UNIX, ...), which prevents wineserver from
 * creating its pathname master socket.  They may still allow
 * socketpair(AF_UNIX, ...), including SCM_RIGHTS descriptor passing.
 *
 * This preload library translates only pathname AF_UNIX/SOCK_STREAM sockets
 * into loopback TCP sockets.  Every translated TCP connection receives a
 * native AF_UNIX socketpair side channel during connect/accept.  The far side
 * of that pair is copied once with pidfd_getfd().  Wine's sendmsg()/recvmsg()
 * traffic then travels unchanged over the side channel, while a byte on the
 * TCP socket mirrors message readiness for Wine's existing poll loop.
 */

#define MAX_TRACKED_FDS 65536
#define RENDEZVOUS_MAGIC UINT32_C(0x57545231) /* WTR1 */
#define HELLO_MAGIC      UINT32_C(0x57544831) /* WTH1 */
#define ACK_MAGIC        UINT32_C(0x57544131) /* WTA1 */
#define PROTOCOL_VERSION 1
#define TOKEN_BYTE       UINT8_C(0xa7)
#define DEFAULT_IO_TIMEOUT_MS 1000
#define SIDECAR_SUFFIX   ".winetcp"

enum fd_kind
{
    FD_NONE = 0,
    FD_CANDIDATE,
    FD_BOUND,
    FD_LISTENER,
    FD_CONNECTED
};

struct fd_state
{
    enum fd_kind kind;
    int side_fd;
    int passcred;
    unsigned int pending_tokens;
    uint64_t cookie;
    char path[sizeof(((struct sockaddr_un *)0)->sun_path)];
};

struct rendezvous_wire
{
    uint32_t magic_be;
    uint16_t version_be;
    uint16_t port_be;
    uint64_t cookie_be;
} __attribute__((packed));

struct hello_wire
{
    uint32_t magic_be;
    uint16_t version_be;
    uint16_t reserved_be;
    uint32_t pid_be;
    uint32_t exported_fd_be;
    uint64_t cookie_be;
} __attribute__((packed));

struct ack_wire
{
    uint32_t magic_be;
    uint32_t status_be;
} __attribute__((packed));

static struct fd_state fd_states[MAX_TRACKED_FDS];
static pthread_mutex_t state_mutex = PTHREAD_MUTEX_INITIALIZER;
static pthread_once_t symbols_once = PTHREAD_ONCE_INIT;
static int debug_enabled;
static int process_is_wineserver;
static int translate_all;
static int io_timeout_ms = DEFAULT_IO_TIMEOUT_MS;

static int (*next_socket)(int, int, int);
static int (*next_socketpair)(int, int, int, int[2]);
static int (*next_bind)(int, const struct sockaddr *, socklen_t);
static int (*next_listen)(int, int);
static int (*next_connect)(int, const struct sockaddr *, socklen_t);
static int (*next_accept)(int, struct sockaddr *, socklen_t *);
static int (*next_accept4)(int, struct sockaddr *, socklen_t *, int);
static int (*next_setsockopt)(int, int, int, const void *, socklen_t);
static int (*next_getsockname)(int, struct sockaddr *, socklen_t *);
static ssize_t (*next_sendmsg)(int, const struct msghdr *, int);
static ssize_t (*next_recvmsg)(int, struct msghdr *, int);
static int (*next_close)(int);
static int (*next_unlink)(const char *);
static int (*next_dup)(int);
static int (*next_dup2)(int, int);
static int (*next_dup3)(int, int, int);
static int (*next_fcntl)(int, int, ...);

static void resolve_symbols_once(void)
{
#define RESOLVE(name) do { *(void **)(&next_##name) = dlsym(RTLD_NEXT, #name); } while (0)
    RESOLVE(socket);
    RESOLVE(socketpair);
    RESOLVE(bind);
    RESOLVE(listen);
    RESOLVE(connect);
    RESOLVE(accept);
    RESOLVE(accept4);
    RESOLVE(setsockopt);
    RESOLVE(getsockname);
    RESOLVE(sendmsg);
    RESOLVE(recvmsg);
    RESOLVE(close);
    RESOLVE(unlink);
    RESOLVE(dup);
    RESOLVE(dup2);
    RESOLVE(dup3);
    RESOLVE(fcntl);
#undef RESOLVE

    if (!next_socket || !next_socketpair || !next_bind || !next_listen ||
        !next_connect || !next_accept || !next_setsockopt ||
        !next_getsockname || !next_sendmsg || !next_recvmsg || !next_close ||
        !next_unlink || !next_dup || !next_dup2 || !next_fcntl)
    {
        static const char msg[] = "wine-tcp-preload: failed to resolve libc symbols\n";
        ssize_t ignored = write(STDERR_FILENO, msg, sizeof(msg) - 1);
        (void)ignored;
        _exit(127);
    }
}

static inline void ensure_symbols(void)
{
    pthread_once(&symbols_once, resolve_symbols_once);
}

static void debug_log(const char *fmt, ...)
{
    va_list args;

    if (!debug_enabled) return;
    dprintf(STDERR_FILENO, "wine-tcp-preload[%ld]: ", (long)getpid());
    va_start(args, fmt);
    vdprintf(STDERR_FILENO, fmt, args);
    va_end(args);
}

static bool should_translate_socket(void *caller)
{
    Dl_info info;
    const char *name;

    if (translate_all || process_is_wineserver) return true;
    memset(&info, 0, sizeof(info));
    if (!dladdr(caller, &info) || !info.dli_fname) return false;
    name = strrchr(info.dli_fname, '/');
    name = name ? name + 1 : info.dli_fname;
    return !strcmp(name, "ntdll.so");
}

static void atfork_prepare(void)
{
    pthread_mutex_lock(&state_mutex);
}

static void atfork_parent(void)
{
    pthread_mutex_unlock(&state_mutex);
}

static void atfork_child(void)
{
    pthread_mutex_unlock(&state_mutex);
}

static bool fd_in_range(int fd)
{
    return fd >= 0 && fd < MAX_TRACKED_FDS;
}

static struct fd_state get_state(int fd)
{
    struct fd_state state;

    memset(&state, 0, sizeof(state));
    state.side_fd = -1;
    if (!fd_in_range(fd)) return state;
    pthread_mutex_lock(&state_mutex);
    state = fd_states[fd];
    pthread_mutex_unlock(&state_mutex);
    return state;
}

static void set_state(int fd, const struct fd_state *state)
{
    if (!fd_in_range(fd)) return;
    pthread_mutex_lock(&state_mutex);
    fd_states[fd] = *state;
    pthread_mutex_unlock(&state_mutex);
}

static int clear_state(int fd)
{
    int side_fd = -1;

    if (!fd_in_range(fd)) return -1;
    pthread_mutex_lock(&state_mutex);
    if (fd_states[fd].kind == FD_CONNECTED) side_fd = fd_states[fd].side_fd;
    memset(&fd_states[fd], 0, sizeof(fd_states[fd]));
    fd_states[fd].side_fd = -1;
    pthread_mutex_unlock(&state_mutex);
    return side_fd;
}

static bool take_pending_token(int fd)
{
    bool ret = false;

    if (!fd_in_range(fd)) return false;
    pthread_mutex_lock(&state_mutex);
    if (fd_states[fd].pending_tokens)
    {
        fd_states[fd].pending_tokens--;
        ret = true;
    }
    pthread_mutex_unlock(&state_mutex);
    return ret;
}

static void restore_pending_token(int fd)
{
    if (!fd_in_range(fd)) return;
    pthread_mutex_lock(&state_mutex);
    fd_states[fd].pending_tokens++;
    pthread_mutex_unlock(&state_mutex);
}

static uint64_t random_cookie(void)
{
    uint64_t cookie;

    if (getrandom(&cookie, sizeof(cookie), 0) == (ssize_t)sizeof(cookie) && cookie)
        return cookie;
    cookie = ((uint64_t)(unsigned long)getpid() << 32) ^
             (uint64_t)(uintptr_t)&cookie ^ (uint64_t)time(NULL);
    return cookie ? cookie : UINT64_C(0x9f37d1256ac48b01);
}

static int64_t monotonic_milliseconds(void)
{
    struct timespec ts;

    if (clock_gettime(CLOCK_MONOTONIC, &ts) < 0) return -1;
    return (int64_t)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

static int wait_for_fd_until(int fd, short events, int64_t deadline)
{
    struct pollfd pfd = { .fd = fd, .events = events };
    int64_t now;
    int timeout;
    int ret;

    now = monotonic_milliseconds();
    if (now < 0) return -1;
    if (now >= deadline)
    {
        errno = ETIMEDOUT;
        return -1;
    }
    timeout = deadline - now > INT_MAX ? INT_MAX : (int)(deadline - now);
    do ret = poll(&pfd, 1, timeout); while (ret < 0 && errno == EINTR);
    if (!ret)
    {
        errno = ETIMEDOUT;
        return -1;
    }
    if (ret < 0) return -1;
    if (pfd.revents & (POLLERR | POLLNVAL))
    {
        errno = ECONNRESET;
        return -1;
    }
    return 0;
}

static int send_full(int fd, const void *buf, size_t size)
{
    const unsigned char *ptr = buf;
    int64_t deadline = monotonic_milliseconds();

    if (deadline < 0) return -1;
    deadline += io_timeout_ms;

    while (size)
    {
        ssize_t ret = send(fd, ptr, size, MSG_NOSIGNAL | MSG_DONTWAIT);
        if (ret > 0)
        {
            ptr += ret;
            size -= (size_t)ret;
            continue;
        }
        if (!ret)
        {
            errno = EPIPE;
            return -1;
        }
        if (errno == EINTR) continue;
        if ((errno == EAGAIN || errno == EWOULDBLOCK) &&
            !wait_for_fd_until(fd, POLLOUT, deadline)) continue;
        return -1;
    }
    return 0;
}

static int recv_full(int fd, void *buf, size_t size)
{
    unsigned char *ptr = buf;
    int64_t deadline = monotonic_milliseconds();

    if (deadline < 0) return -1;
    deadline += io_timeout_ms;

    while (size)
    {
        ssize_t ret = recv(fd, ptr, size, MSG_DONTWAIT);
        if (ret > 0)
        {
            ptr += ret;
            size -= (size_t)ret;
            continue;
        }
        if (!ret)
        {
            errno = ECONNRESET;
            return -1;
        }
        if (errno == EINTR) continue;
        if ((errno == EAGAIN || errno == EWOULDBLOCK) &&
            !wait_for_fd_until(fd, POLLIN, deadline)) continue;
        return -1;
    }
    return 0;
}

static int pidfd_open_raw(pid_t pid)
{
#ifdef SYS_pidfd_open
    return (int)syscall(SYS_pidfd_open, pid, 0u);
#else
    (void)pid;
    errno = ENOSYS;
    return -1;
#endif
}

static int pidfd_getfd_raw(int pidfd, int target_fd)
{
#ifdef SYS_pidfd_getfd
    return (int)syscall(SYS_pidfd_getfd, pidfd, target_fd, 0u);
#else
    (void)pidfd;
    (void)target_fd;
    errno = ENOSYS;
    return -1;
#endif
}

static int set_tcp_nodelay(int fd)
{
    int one = 1;
    return next_setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
}

static int make_sidecar_path(const char *path, char *sidecar, size_t size)
{
    int ret = snprintf(sidecar, size, "%s%s", path, SIDECAR_SUFFIX);
    if (ret < 0 || (size_t)ret >= size)
    {
        errno = ENAMETOOLONG;
        return -1;
    }
    return 0;
}

static int write_all_fd(int fd, const void *buf, size_t size)
{
    const unsigned char *ptr = buf;

    while (size)
    {
        ssize_t ret = write(fd, ptr, size);
        if (ret > 0)
        {
            ptr += ret;
            size -= (size_t)ret;
            continue;
        }
        if (ret < 0 && errno == EINTR) continue;
        if (!ret) errno = EIO;
        return -1;
    }
    return 0;
}

static int read_all_fd(int fd, void *buf, size_t size)
{
    unsigned char *ptr = buf;

    while (size)
    {
        ssize_t ret = read(fd, ptr, size);
        if (ret > 0)
        {
            ptr += ret;
            size -= (size_t)ret;
            continue;
        }
        if (ret < 0 && errno == EINTR) continue;
        errno = ret ? errno : EPROTO;
        return -1;
    }
    return 0;
}

static int create_socket_marker(const char *path)
{
    struct sockaddr_un addr;
    int pair[2] = {-1, -1};
    socklen_t len;
    int saved_errno;

    if (next_socketpair(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0, pair) < 0) return -1;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    if (strlen(path) >= sizeof(addr.sun_path))
    {
        saved_errno = ENAMETOOLONG;
        goto error;
    }
    strcpy(addr.sun_path, path);
    len = (socklen_t)(offsetof(struct sockaddr_un, sun_path) + strlen(path) + 1);
    if (next_bind(pair[0], (const struct sockaddr *)&addr, len) < 0)
    {
        saved_errno = errno;
        goto error;
    }
    if (chmod(path, 0600) < 0)
    {
        saved_errno = errno;
        goto error;
    }
    next_close(pair[0]);
    next_close(pair[1]);
    return 0;

error:
    if (pair[0] >= 0) next_close(pair[0]);
    if (pair[1] >= 0) next_close(pair[1]);
    errno = saved_errno;
    return -1;
}

static int publish_rendezvous(const struct fd_state *state, uint16_t port)
{
    struct rendezvous_wire wire;
    char sidecar[PATH_MAX];
    char temporary[PATH_MAX];
    int fd = -1;
    int saved_errno;

    if (make_sidecar_path(state->path, sidecar, sizeof(sidecar)) < 0) return -1;
    if (snprintf(temporary, sizeof(temporary), "%s.tmp.%ld", sidecar, (long)getpid()) >=
        (int)sizeof(temporary))
    {
        errno = ENAMETOOLONG;
        return -1;
    }

    memset(&wire, 0, sizeof(wire));
    wire.magic_be = htonl(RENDEZVOUS_MAGIC);
    wire.version_be = htons(PROTOCOL_VERSION);
    wire.port_be = htons(port);
    wire.cookie_be = htobe64(state->cookie);

    (void)next_unlink(temporary);
    fd = open(temporary, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0600);
    if (fd < 0) return -1;
    if (write_all_fd(fd, &wire, sizeof(wire)) < 0)
    {
        saved_errno = errno;
        goto error;
    }
    if (next_close(fd) < 0)
    {
        fd = -1;
        saved_errno = errno;
        goto error;
    }
    fd = -1;
    if (rename(temporary, sidecar) < 0)
    {
        saved_errno = errno;
        goto error;
    }
    return 0;

error:
    if (fd >= 0) next_close(fd);
    (void)next_unlink(temporary);
    errno = saved_errno;
    return -1;
}

static int read_rendezvous(const char *path, struct rendezvous_wire *wire)
{
    char sidecar[PATH_MAX];
    int fd;
    int saved_errno;

    if (make_sidecar_path(path, sidecar, sizeof(sidecar)) < 0) return -1;
    fd = open(sidecar, O_RDONLY | O_CLOEXEC);
    if (fd < 0) return -1;
    if (read_all_fd(fd, wire, sizeof(*wire)) < 0)
    {
        saved_errno = errno;
        next_close(fd);
        errno = saved_errno;
        return -1;
    }
    next_close(fd);
    if (ntohl(wire->magic_be) != RENDEZVOUS_MAGIC ||
        ntohs(wire->version_be) != PROTOCOL_VERSION || !ntohs(wire->port_be))
    {
        errno = EPROTO;
        return -1;
    }
    return 0;
}

static void cleanup_sidecar(const char *path)
{
    struct rendezvous_wire wire;
    struct stat st;
    char sidecar[PATH_MAX];
    int fd;

    if (make_sidecar_path(path, sidecar, sizeof(sidecar)) < 0) return;
    fd = open(sidecar, O_RDONLY | O_CLOEXEC | O_NONBLOCK | O_NOFOLLOW);
    if (fd < 0) return;
    if (!fstat(fd, &st) && S_ISREG(st.st_mode) && st.st_size == sizeof(wire) &&
        st.st_uid == geteuid() && !read_all_fd(fd, &wire, sizeof(wire)) &&
        ntohl(wire.magic_be) == RENDEZVOUS_MAGIC)
        (void)next_unlink(sidecar);
    next_close(fd);
}

static int install_server_side_channel(int accepted_fd, const struct fd_state *listener)
{
    struct hello_wire hello;
    struct ack_wire ack;
    struct fd_state state;
    int pidfd = -1;
    int side_fd = -1;
    int status = 0;

    if (recv_full(accepted_fd, &hello, sizeof(hello)) < 0) return -1;
    if (ntohl(hello.magic_be) != HELLO_MAGIC ||
        ntohs(hello.version_be) != PROTOCOL_VERSION ||
        be64toh(hello.cookie_be) != listener->cookie ||
        !(pid_t)ntohl(hello.pid_be))
    {
        status = EPROTO;
    }
    else
    {
        pidfd = pidfd_open_raw((pid_t)ntohl(hello.pid_be));
        if (pidfd < 0) status = errno;
        else
        {
            side_fd = pidfd_getfd_raw(pidfd, (int)ntohl(hello.exported_fd_be));
            if (side_fd < 0) status = errno;
        }
    }
    if (pidfd >= 0) next_close(pidfd);
    if (side_fd >= 0 && fcntl(side_fd, F_SETFD, FD_CLOEXEC) < 0)
    {
        status = errno;
        next_close(side_fd);
        side_fd = -1;
    }

    ack.magic_be = htonl(ACK_MAGIC);
    ack.status_be = htonl((uint32_t)status);
    if (send_full(accepted_fd, &ack, sizeof(ack)) < 0)
    {
        if (side_fd >= 0) next_close(side_fd);
        return -1;
    }
    if (status)
    {
        errno = status;
        return -1;
    }

    memset(&state, 0, sizeof(state));
    state.kind = FD_CONNECTED;
    state.side_fd = side_fd;
    state.cookie = listener->cookie;
    set_state(accepted_fd, &state);
    set_tcp_nodelay(accepted_fd);
    debug_log("accepted TCP master fd %d with Unix side fd %d\n", accepted_fd, side_fd);
    return 0;
}

static int fill_unix_peer(struct sockaddr *address, socklen_t *address_len)
{
    struct sockaddr_un peer;
    socklen_t copy_len;

    if (!address || !address_len) return 0;
    memset(&peer, 0, sizeof(peer));
    peer.sun_family = AF_UNIX;
    copy_len = *address_len < sizeof(peer) ? *address_len : sizeof(peer);
    memcpy(address, &peer, copy_len);
    *address_len = sizeof(peer);
    return 0;
}

static int copy_state_to_duplicate(int old_fd, int new_fd, bool close_on_exec)
{
    struct fd_state state = get_state(old_fd);
    int command;

    if (state.kind == FD_NONE) return 0;
    if (state.kind == FD_CONNECTED)
    {
        command = close_on_exec ? F_DUPFD_CLOEXEC : F_DUPFD;
        state.side_fd = next_fcntl(state.side_fd, command, 0);
        if (state.side_fd < 0) return -1;
        state.pending_tokens = 0;
    }
    set_state(new_fd, &state);
    debug_log("copied translated fd state %d -> %d (side fd %d)\n",
              old_fd, new_fd, state.side_fd);
    return 0;
}

static bool fcntl_has_no_argument(int command)
{
    switch (command)
    {
    case F_GETFD:
    case F_GETFL:
    case F_GETOWN:
#ifdef F_GETSIG
    case F_GETSIG:
#endif
#ifdef F_GETLEASE
    case F_GETLEASE:
#endif
#ifdef F_GETPIPE_SZ
    case F_GETPIPE_SZ:
#endif
#ifdef F_GET_SEALS
    case F_GET_SEALS:
#endif
        return true;
    default:
        return false;
    }
}

static bool fcntl_has_integer_argument(int command)
{
    switch (command)
    {
    case F_DUPFD:
#ifdef F_DUPFD_CLOEXEC
    case F_DUPFD_CLOEXEC:
#endif
    case F_SETFD:
    case F_SETFL:
    case F_SETOWN:
#ifdef F_SETSIG
    case F_SETSIG:
#endif
#ifdef F_SETLEASE
    case F_SETLEASE:
#endif
#ifdef F_NOTIFY
    case F_NOTIFY:
#endif
#ifdef F_SETPIPE_SZ
    case F_SETPIPE_SZ:
#endif
#ifdef F_ADD_SEALS
    case F_ADD_SEALS:
#endif
        return true;
    default:
        return false;
    }
}

__attribute__((constructor)) static void preload_init(void)
{
    const char *debug = getenv("WINE_TCP_PRELOAD_DEBUG");
    const char *test_mode = getenv("WINE_TCP_PRELOAD_TRANSLATE_ALL");
    const char *timeout_value = getenv("WINE_TCP_PRELOAD_HANDSHAKE_TIMEOUT_MS");
    char executable[PATH_MAX];
    const char *name;
    ssize_t len;

    debug_enabled = debug && *debug && strcmp(debug, "0");
    translate_all = test_mode && *test_mode && strcmp(test_mode, "0");
    if (timeout_value && *timeout_value)
    {
        char *end;
        long value = strtol(timeout_value, &end, 10);
        if (!*end && value >= 10 && value <= 60000) io_timeout_ms = (int)value;
    }
    len = readlink("/proc/self/exe", executable, sizeof(executable) - 1);
    if (len > 0)
    {
        executable[len] = 0;
        name = strrchr(executable, '/');
        name = name ? name + 1 : executable;
        process_is_wineserver = !strcmp(name, "wineserver");
    }
    ensure_symbols();
    if (pthread_atfork(atfork_prepare, atfork_parent, atfork_child))
    {
        static const char msg[] = "wine-tcp-preload: pthread_atfork failed\n";
        ssize_t ignored = write(STDERR_FILENO, msg, sizeof(msg) - 1);
        (void)ignored;
        _exit(127);
    }
    debug_log("loaded (wineserver=%d, translate_all=%d, handshake_timeout_ms=%d)\n",
              process_is_wineserver, translate_all, io_timeout_ms);
}

int socket(int domain, int type, int protocol)
{
    struct fd_state state;
    int fd;

    ensure_symbols();
    if (domain != AF_UNIX || (type & 0xf) != SOCK_STREAM ||
        !should_translate_socket(__builtin_return_address(0)))
        return next_socket(domain, type, protocol);

    fd = next_socket(AF_INET, type, 0);
    if (fd < 0) return fd;
    memset(&state, 0, sizeof(state));
    state.kind = FD_CANDIDATE;
    state.side_fd = -1;
    set_state(fd, &state);
    debug_log("translated AF_UNIX socket to TCP fd %d\n", fd);
    return fd;
}

int bind(int fd, const struct sockaddr *address, socklen_t address_len)
{
    const struct sockaddr_un *unix_address = (const struct sockaddr_un *)address;
    struct sockaddr_in inet_address;
    struct fd_state state;
    int ret;

    ensure_symbols();
    state = get_state(fd);
    if (state.kind != FD_CANDIDATE || !address || address->sa_family != AF_UNIX)
        return next_bind(fd, address, address_len);
    if (!unix_address->sun_path[0])
    {
        errno = EAFNOSUPPORT;
        return -1;
    }
    if (strnlen(unix_address->sun_path, sizeof(unix_address->sun_path)) >= sizeof(state.path))
    {
        errno = ENAMETOOLONG;
        return -1;
    }

    memset(&inet_address, 0, sizeof(inet_address));
    inet_address.sin_family = AF_INET;
    inet_address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    inet_address.sin_port = 0;
    ret = next_bind(fd, (const struct sockaddr *)&inet_address, sizeof(inet_address));
    if (ret < 0) return ret;

    state.kind = FD_BOUND;
    state.cookie = random_cookie();
    strcpy(state.path, unix_address->sun_path);
    if (create_socket_marker(state.path) < 0) return -1;
    set_state(fd, &state);
    debug_log("bound TCP fd %d for Unix path %s\n", fd, state.path);
    return 0;
}

int listen(int fd, int backlog)
{
    struct sockaddr_in address;
    struct fd_state state;
    socklen_t address_len = sizeof(address);
    int ret;

    ensure_symbols();
    state = get_state(fd);
    if (state.kind != FD_BOUND) return next_listen(fd, backlog);

    ret = next_listen(fd, backlog);
    if (ret < 0) return ret;
    if (next_getsockname(fd, (struct sockaddr *)&address, &address_len) < 0) return -1;
    if (publish_rendezvous(&state, ntohs(address.sin_port)) < 0) return -1;
    state.kind = FD_LISTENER;
    set_state(fd, &state);
    debug_log("listening for %s on 127.0.0.1:%u\n", state.path,
              (unsigned)ntohs(address.sin_port));
    return 0;
}

int connect(int fd, const struct sockaddr *address, socklen_t address_len)
{
    const struct sockaddr_un *unix_address = (const struct sockaddr_un *)address;
    struct rendezvous_wire rendezvous;
    struct sockaddr_in inet_address;
    struct hello_wire hello;
    struct ack_wire ack;
    struct fd_state state;
    int pair[2] = {-1, -1};
    int saved_errno;
    int status;

    ensure_symbols();
    state = get_state(fd);
    if (state.kind != FD_CANDIDATE || !address || address->sa_family != AF_UNIX)
        return next_connect(fd, address, address_len);
    if (!unix_address->sun_path[0])
    {
        errno = EAFNOSUPPORT;
        return -1;
    }
    if (read_rendezvous(unix_address->sun_path, &rendezvous) < 0) return -1;

    memset(&inet_address, 0, sizeof(inet_address));
    inet_address.sin_family = AF_INET;
    inet_address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    inet_address.sin_port = rendezvous.port_be;
    if (next_connect(fd, (const struct sockaddr *)&inet_address, sizeof(inet_address)) < 0) return -1;
    set_tcp_nodelay(fd);

    if (next_socketpair(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0, pair) < 0) return -1;
    if (state.passcred && next_setsockopt(pair[0], SOL_SOCKET, SO_PASSCRED,
                                         &state.passcred, sizeof(state.passcred)) < 0)
    {
        saved_errno = errno;
        goto error;
    }

    memset(&hello, 0, sizeof(hello));
    hello.magic_be = htonl(HELLO_MAGIC);
    hello.version_be = htons(PROTOCOL_VERSION);
    hello.pid_be = htonl((uint32_t)getpid());
    hello.exported_fd_be = htonl((uint32_t)pair[1]);
    hello.cookie_be = rendezvous.cookie_be;
    if (send_full(fd, &hello, sizeof(hello)) < 0 || recv_full(fd, &ack, sizeof(ack)) < 0)
    {
        saved_errno = errno;
        goto error;
    }
    if (ntohl(ack.magic_be) != ACK_MAGIC)
    {
        saved_errno = EPROTO;
        goto error;
    }
    status = (int)ntohl(ack.status_be);
    if (status)
    {
        saved_errno = status;
        goto error;
    }

    next_close(pair[1]);
    state.kind = FD_CONNECTED;
    state.side_fd = pair[0];
    state.cookie = be64toh(rendezvous.cookie_be);
    state.pending_tokens = 0;
    set_state(fd, &state);
    debug_log("connected TCP master fd %d with Unix side fd %d\n", fd, pair[0]);
    return 0;

error:
    if (pair[0] >= 0) next_close(pair[0]);
    if (pair[1] >= 0) next_close(pair[1]);
    errno = saved_errno;
    return -1;
}

int accept(int fd, struct sockaddr *address, socklen_t *address_len)
{
    struct sockaddr_storage ignored;
    struct fd_state state;
    socklen_t ignored_len = sizeof(ignored);
    int accepted;
    int saved_errno;

    ensure_symbols();
    state = get_state(fd);
    if (state.kind != FD_LISTENER) return next_accept(fd, address, address_len);
    accepted = next_accept(fd, (struct sockaddr *)&ignored, &ignored_len);
    if (accepted < 0) return accepted;
    if (install_server_side_channel(accepted, &state) < 0)
    {
        saved_errno = errno;
        next_close(accepted);
        errno = saved_errno;
        return -1;
    }
    fill_unix_peer(address, address_len);
    return accepted;
}

int accept4(int fd, struct sockaddr *address, socklen_t *address_len, int flags)
{
    struct sockaddr_storage ignored;
    struct fd_state state;
    socklen_t ignored_len = sizeof(ignored);
    int accepted;
    int saved_errno;

    ensure_symbols();
    state = get_state(fd);
    if (state.kind != FD_LISTENER)
    {
        if (next_accept4) return next_accept4(fd, address, address_len, flags);
        errno = ENOSYS;
        return -1;
    }
    if (next_accept4)
        accepted = next_accept4(fd, (struct sockaddr *)&ignored, &ignored_len, flags);
    else
        accepted = next_accept(fd, (struct sockaddr *)&ignored, &ignored_len);
    if (accepted < 0) return accepted;
    if (install_server_side_channel(accepted, &state) < 0)
    {
        saved_errno = errno;
        next_close(accepted);
        errno = saved_errno;
        return -1;
    }
    fill_unix_peer(address, address_len);
    return accepted;
}

int setsockopt(int fd, int level, int option, const void *value, socklen_t value_len)
{
    struct fd_state state;
    int enabled;

    ensure_symbols();
    state = get_state(fd);
    if (level != SOL_SOCKET || option != SO_PASSCRED || state.kind == FD_NONE)
        return next_setsockopt(fd, level, option, value, value_len);
    if (!value || value_len < sizeof(enabled))
    {
        errno = EINVAL;
        return -1;
    }
    memcpy(&enabled, value, sizeof(enabled));
    state.passcred = !!enabled;
    set_state(fd, &state);
    if (state.side_fd >= 0)
        return next_setsockopt(state.side_fd, level, option, &state.passcred, sizeof(state.passcred));
    return 0;
}

ssize_t sendmsg(int fd, const struct msghdr *message, int flags)
{
    struct fd_state state;
    uint8_t token = TOKEN_BYTE;
    ssize_t ret;
    int saved_errno;

    ensure_symbols();
    state = get_state(fd);
    if (state.kind != FD_CONNECTED || state.side_fd < 0)
        return next_sendmsg(fd, message, flags);

    ret = next_sendmsg(state.side_fd, message, flags);
    if (ret <= 0) return ret;
    if (send_full(fd, &token, sizeof(token)) < 0)
    {
        saved_errno = errno;
        errno = saved_errno;
        return -1;
    }
    debug_log("forwarded sendmsg fd %d via side fd %d (%zd bytes)\n", fd, state.side_fd, ret);
    return ret;
}

ssize_t recvmsg(int fd, struct msghdr *message, int flags)
{
    struct fd_state state;
    uint8_t token;
    ssize_t ret;
    int recv_flags = flags & (MSG_DONTWAIT | MSG_PEEK);
    int saved_errno;
    bool had_pending;
    bool token_consumed;

    ensure_symbols();
    state = get_state(fd);
    if (state.kind != FD_CONNECTED || state.side_fd < 0)
        return next_recvmsg(fd, message, flags);

    had_pending = take_pending_token(fd);
    token_consumed = had_pending;
    if (!had_pending)
    {
        ret = recv(fd, &token, sizeof(token), recv_flags);
        if (ret <= 0) return ret;
        if (token != TOKEN_BYTE)
        {
            errno = EPROTO;
            return -1;
        }
        token_consumed = !(flags & MSG_PEEK);
    }

    ret = next_recvmsg(state.side_fd, message, flags);
    if (token_consumed &&
        ((ret < 0 && (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK)) ||
         (ret >= 0 && (flags & MSG_PEEK))))
    {
        saved_errno = errno;
        restore_pending_token(fd);
        errno = saved_errno;
    }
    if (ret >= 0)
        debug_log("forwarded recvmsg fd %d via side fd %d (%zd bytes)\n", fd, state.side_fd, ret);
    return ret;
}

int dup(int old_fd)
{
    int new_fd;
    int saved_errno;

    ensure_symbols();
    new_fd = next_dup(old_fd);
    if (new_fd < 0) return new_fd;
    if (copy_state_to_duplicate(old_fd, new_fd, false) < 0)
    {
        saved_errno = errno;
        next_close(new_fd);
        errno = saved_errno;
        return -1;
    }
    return new_fd;
}

int dup2(int old_fd, int new_fd)
{
    struct fd_state source_state;
    int replaced_side;
    int minimum_side_fd;
    int ret;
    int saved_errno;

    ensure_symbols();
    if (old_fd == new_fd) return next_dup2(old_fd, new_fd);
    source_state = get_state(old_fd);
    if (source_state.kind == FD_CONNECTED)
    {
        minimum_side_fd = new_fd < INT_MAX ? new_fd + 1 : 0;
        source_state.side_fd = next_fcntl(source_state.side_fd, F_DUPFD, minimum_side_fd);
        if (source_state.side_fd < 0) return -1;
        source_state.pending_tokens = 0;
    }
    ret = next_dup2(old_fd, new_fd);
    if (ret < 0)
    {
        saved_errno = errno;
        if (source_state.kind == FD_CONNECTED) next_close(source_state.side_fd);
        errno = saved_errno;
        return -1;
    }
    replaced_side = clear_state(new_fd);
    if (replaced_side >= 0 && replaced_side != new_fd) next_close(replaced_side);
    if (source_state.kind != FD_NONE) set_state(new_fd, &source_state);
    return ret;
}

int dup3(int old_fd, int new_fd, int flags)
{
    struct fd_state source_state;
    int replaced_side;
    int minimum_side_fd;
    int duplicate_command;
    int ret;
    int saved_errno;

    ensure_symbols();
    if (!next_dup3)
    {
        errno = ENOSYS;
        return -1;
    }
    if (old_fd == new_fd) return next_dup3(old_fd, new_fd, flags);
    source_state = get_state(old_fd);
    if (source_state.kind == FD_CONNECTED)
    {
        minimum_side_fd = new_fd < INT_MAX ? new_fd + 1 : 0;
        duplicate_command = flags & O_CLOEXEC ? F_DUPFD_CLOEXEC : F_DUPFD;
        source_state.side_fd = next_fcntl(source_state.side_fd, duplicate_command,
                                          minimum_side_fd);
        if (source_state.side_fd < 0) return -1;
        source_state.pending_tokens = 0;
    }
    ret = next_dup3(old_fd, new_fd, flags);
    if (ret < 0)
    {
        saved_errno = errno;
        if (source_state.kind == FD_CONNECTED) next_close(source_state.side_fd);
        errno = saved_errno;
        return -1;
    }
    replaced_side = clear_state(new_fd);
    if (replaced_side >= 0 && replaced_side != new_fd) next_close(replaced_side);
    if (source_state.kind != FD_NONE) set_state(new_fd, &source_state);
    return ret;
}

int fcntl(int fd, int command, ...)
{
    struct fd_state state;
    va_list args;
    void *pointer_argument;
    int integer_argument;
    int new_fd;
    int ret;
    int saved_errno;

    ensure_symbols();
    if (fcntl_has_no_argument(command)) return next_fcntl(fd, command);

    va_start(args, command);
    if (fcntl_has_integer_argument(command))
    {
        integer_argument = va_arg(args, int);
        va_end(args);

        if (command == F_DUPFD
#ifdef F_DUPFD_CLOEXEC
            || command == F_DUPFD_CLOEXEC
#endif
           )
        {
            new_fd = next_fcntl(fd, command, integer_argument);
            if (new_fd < 0) return new_fd;
            if (copy_state_to_duplicate(fd, new_fd,
#ifdef F_DUPFD_CLOEXEC
                                        command == F_DUPFD_CLOEXEC
#else
                                        false
#endif
                                       ) < 0)
            {
                saved_errno = errno;
                next_close(new_fd);
                errno = saved_errno;
                return -1;
            }
            return new_fd;
        }

        ret = next_fcntl(fd, command, integer_argument);
        if (ret < 0 || (command != F_SETFL && command != F_SETFD)) return ret;
        state = get_state(fd);
        if (state.kind == FD_CONNECTED && state.side_fd >= 0)
        {
            if (next_fcntl(state.side_fd, command, integer_argument) < 0) return -1;
        }
        return ret;
    }

    pointer_argument = va_arg(args, void *);
    va_end(args);
    return next_fcntl(fd, command, pointer_argument);
}

int close(int fd)
{
    int side_fd;
    int ret;
    int saved_errno;

    ensure_symbols();
    side_fd = clear_state(fd);
    if (side_fd >= 0 && side_fd != fd) (void)next_close(side_fd);
    ret = next_close(fd);
    saved_errno = errno;
    errno = saved_errno;
    return ret;
}

int unlink(const char *path)
{
    int ret;
    int saved_errno;

    ensure_symbols();
    ret = next_unlink(path);
    saved_errno = errno;
    /* Wine's master pathname is the literal relative path "socket".  Do not
     * inspect arbitrary sibling paths: wineserver also unlinks ordinary Wine
     * files on behalf of clients.  Test mode retains generic-path cleanup. */
    if (!ret && ((process_is_wineserver && !strcmp(path, "socket")) ||
                 (translate_all && *path)))
        cleanup_sidecar(path);
    errno = saved_errno;
    return ret;
}
