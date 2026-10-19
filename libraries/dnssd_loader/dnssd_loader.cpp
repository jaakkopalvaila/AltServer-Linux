#include "dns_sd.h"
#include <iostream>
#include <string>
#include <unistd.h>
#include <sys/prctl.h> // prctl(), PR_SET_PDEATHSIG
#include <signal.h> // signals


DNSServiceErrorType DNSSD_API DNSServiceRegister
    (
    DNSServiceRef                       *sdRef,
    DNSServiceFlags                     flags,
    uint32_t                            interfaceIndex,
    const char                          *name,         /* may be NULL */
    const char                          *regtype,
    const char                          *domain,       /* may be NULL */
    const char                          *host,         /* may be NULL */
    uint16_t                            port,
    uint16_t                            txtLen,
    const void                          *txtRecord,    /* may be NULL */
    DNSServiceRegisterReply             callBack,      /* may be NULL */
    void                                *context       /* may be NULL */
    ) {
        // python3 -c 'from ctypes import *; sdRef = c_void_p(); CDLL('libdns_sd.so').DNSServiceRegister(byref(sdRef), flags, interfaceIndex, name, regtype, domain, host, txtLen, txtRecord, None, None)'
        // Distributions ship the versioned libdns_sd.so.1 in the runtime package (Debian/Ubuntu:
        // libavahi-compat-libdnssd1); the unversioned libdns_sd.so symlink only comes with the -dev package.
        std::string pyCommand = "from ctypes import *\n"
            "try: dll = CDLL('libdns_sd.so.1')\n"
            "except OSError: dll = CDLL('libdns_sd.so')\n";

        // DNSServiceRef is a pointer. A c_int holds 4 bytes, so on a 64-bit host the library wrote
        // 8 bytes into a 4-byte buffer.
        pyCommand += "sdRef = c_void_p(); ";
#define INT_ARG(argname) (std::string("") + #argname " = " + std::to_string(argname) + "; ")
#define STR_ARG(argname) (argname ? std::string(#argname " = br'") + argname + "'; " : std::string(#argname " = None; "))
        pyCommand += INT_ARG(flags);
        pyCommand += INT_ARG(interfaceIndex);
        pyCommand += STR_ARG(name);
        pyCommand += STR_ARG(regtype);
        pyCommand += STR_ARG(domain);
        pyCommand += STR_ARG(host);
        pyCommand += INT_ARG(port);
        pyCommand += INT_ARG(txtLen);
        
        std::string txtRecordHex = "";
        for (int i = 0; i < txtLen; i++) {
            char buf[16] = { 0 };
            // unsigned char: where plain char is signed (x86_64, i386), a byte from 0x80 up was
            // printed as \xFFFFFF80, which Python reads as \xFF followed by the text "FFFF80".
            sprintf(buf, "\\x%02X", *((unsigned char *)txtRecord + i));
            txtRecordHex += buf;
        }
        pyCommand += "txtRecord = b'" + txtRecordHex + "'; ";
        pyCommand += "ret = dll.DNSServiceRegister(byref(sdRef), flags, interfaceIndex, name, regtype, domain, host, port, txtLen, txtRecord, None, None); ";
        pyCommand += "print('DNSServiceRegister result: %d' % ret); ";

        pyCommand += "from threading import Event; Event().wait(); ";
        
        printf("Running python3 command to advertise AltServer: %s\n", pyCommand.c_str());

        pid_t ppid_before_fork = getpid();
        int child,status;
        if ((child = fork()) < 0) {
            perror("fork");
            return EXIT_FAILURE;
        }
        if(child == 0){
            int r = prctl(PR_SET_PDEATHSIG, SIGTERM);
            if (r == -1) { perror(0); exit(1); }
            // test in case the original parent exited just
            // before the prctl() call
            if (getppid() != ppid_before_fork)
                exit(1);
            execlp("python3", "python3", "-c", pyCommand.c_str(), NULL);
            exit(1);
        } else {
            ;
        }
        return 0;
    }

int DNSSD_API DNSServiceRefSockFD(DNSServiceRef sdRef) {
    return 0xDEADBEEF;
}