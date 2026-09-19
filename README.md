# minecraft
1. Survival and Creative are independent Minecraft worlds.

2. Survival and Creative MUST consume the same physical mods/ directory.

3. Survival and Creative MUST use the same Minecraft/Forge/Java runtime baseline.

4. Survival and Creative MAY have different config/ contents.

5. Survival and Creative MUST have separate kubejs/ trees.

6. server.properties is explicitly managed for each server.

7. Only Velocity exposes Minecraft to the host network.

8. Survival and Creative are never directly exposed on host ports.

9. BlueMap reads Survival world data only.

10. BlueMap's integrated webserver is accessed only through Nginx.

11. Nginx is the public HTTP endpoint.

12. Docker stdout/stderr stays in Docker's logging system.

13. Only intentional application logs are written under logs/.

14. Generated Minecraft/KubeJS/Forge/BlueMap runtime files should remain in Docker volumes unless there is a concrete reason to expose them.

15. The Git repository contains source/configuration/deployment state, not an accidental mirror of /data.