# Configure a Java runtime for one repository

Provision the JDK on every production worker, then set an exact repository mapping:

```bash
export UTA_REPOSITORY_JAVA_HOMES='{"fd_wmonitor_default_store":"/opt/app/jdks/jdk25"}'
```

Keep `UTA_DAEMON_JAVA_HOME` set to the JDK 8 default. Restart the API trigger and daemon after changing the mapping.

Before enabling a mapping, verify `<java-home>/bin/java` exists and is executable. UTA intentionally rejects an invalid configured runtime instead of falling back to another JDK.
