# Security policy

This tool parses untrusted, possibly malicious disk images. Please report:

* crashes or hangs caused by crafted input;
* any way to make the tool write to its source image or device;
* path traversal or other ways extracted names could escape the output directory.

Report privately through GitHub Security Advisories on this repository rather than in a
public issue.

Maps and extraction manifests contain file names, sizes and timestamps from the evidence.
Treat them with the same care as the evidence itself.
