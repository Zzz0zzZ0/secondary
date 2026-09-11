#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <sys/wait.h>
#include <unistd.h>

static pid_t child_pid = -1;

static void forward_signal(int signal_number) {
    if (child_pid > 0) {
        kill(child_pid, signal_number);
    }
}

int main(int argc, char **argv) {
    if (argc < 2) {
        fprintf(stderr, "missing background command\n");
        return 64;
    }
    signal(SIGINT, forward_signal);
    signal(SIGTERM, forward_signal);
    child_pid = fork();
    if (child_pid == 0) {
        execvp(argv[1], &argv[1]);
        perror("execvp");
        _exit(127);
    }
    if (child_pid < 0) {
        perror("fork");
        return 71;
    }
    int status = 0;
    while (waitpid(child_pid, &status, 0) < 0) {
        if (errno != EINTR) {
            perror("waitpid");
            return 71;
        }
    }
    if (WIFEXITED(status)) {
        return WEXITSTATUS(status);
    }
    return WIFSIGNALED(status) ? 128 + WTERMSIG(status) : 1;
}
