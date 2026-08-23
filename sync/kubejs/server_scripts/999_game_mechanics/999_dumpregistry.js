/*
 * KubeJS 2001.6.5
 * Minecraft 1.20.1 Forge
 *
 * MASTER REGISTRY DUMP
 *
 * No filesystem.
 * No arrays containing the registry.
 * No keySet().
 * No entrySet().
 *
 * Registry values are obtained directly through iterator().
 */

var RegistryDump_BuiltInRegistries =
    Java.loadClass(
        'net.minecraft.core.registries.BuiltInRegistries'
    );


// ============================================================
// LOG
// ============================================================

function RegistryDump_Log(message) {
    console.info('[REGDUMP] ' + message);
}


// ============================================================
// HELPERS
// ============================================================

function RegistryDump_Id(registry, object) {

    try {

        var id = registry.getKey(object);

        if (id != null) {
            return id.toString();
        }

    } catch (e) {
    }

    return 'UNKNOWN';
}


function RegistryDump_Mod(id) {

    if (id == null || id === 'UNKNOWN') {
        return 'UNKNOWN';
    }

    var p = id.indexOf(':');

    if (p < 0) {
        return 'minecraft';
    }

    return id.substring(0, p);
}


function RegistryDump_Name(object) {

    try {

        if (object.getName) {
            return object.getName().getString();
        }

    } catch (e) {
    }

    try {

        if (object.getDescription) {
            return object.getDescription().getString();
        }

    } catch (e) {
    }

    return '';
}


function RegistryDump_Escape(value) {

    if (value == null) {
        return '';
    }

    return String(value)
        .replace(/\\/g, '\\\\')
        .replace(/\|/g, '\\|')
        .replace(/\r/g, '\\r')
        .replace(/\n/g, '\\n');
}


function RegistryDump_Tags(object) {

    var tags = [];

    try {

        var holder =
            object.builtInRegistryHolder();

        if (holder == null) {
            return '';
        }

        var stream =
            holder.tags();

        var iterator =
            stream.iterator();

        while (iterator.hasNext()) {

            var tag =
                iterator.next();

            if (tag != null) {
                tags.push(tag.toString());
            }
        }

        try {
            stream.close();
        } catch (e) {
        }

    } catch (e) {
    }

    return tags.join(',');
}


// ============================================================
// ITEMS
// ============================================================

function RegistryDump_Items() {

    RegistryDump_Log('BEGIN|ITEMS');

    var registry =
        RegistryDump_BuiltInRegistries.ITEM;

    var iterator =
        registry.iterator();

    var count = 0;

    while (iterator.hasNext()) {

        var item =
            iterator.next();

        if (item == null) {
            continue;
        }

        var id =
            RegistryDump_Id(registry, item);

        var name =
            RegistryDump_Name(item);

        var mod =
            RegistryDump_Mod(id);

        var stack = '';

        try {
            stack = item.getMaxStackSize();
        } catch (e) {
        }

        var tags =
            RegistryDump_Tags(item);

        RegistryDump_Log(
            'ITEM|' +
            RegistryDump_Escape(id) +
            '|' +
            RegistryDump_Escape(name) +
            '|' +
            RegistryDump_Escape(mod) +
            '|' +
            RegistryDump_Escape(stack) +
            '|' +
            RegistryDump_Escape(tags)
        );

        count++;
    }

    RegistryDump_Log(
        'END|ITEMS|' + count
    );

    return count;
}


// ============================================================
// BLOCKS
// ============================================================

function RegistryDump_Blocks() {

    RegistryDump_Log('BEGIN|BLOCKS');

    var registry =
        RegistryDump_BuiltInRegistries.BLOCK;

    var iterator =
        registry.iterator();

    var count = 0;

    while (iterator.hasNext()) {

        var block =
            iterator.next();

        if (block == null) {
            continue;
        }

        var id =
            RegistryDump_Id(registry, block);

        var name =
            RegistryDump_Name(block);

        var mod =
            RegistryDump_Mod(id);

        var tags =
            RegistryDump_Tags(block);

        RegistryDump_Log(
            'BLOCK|' +
            RegistryDump_Escape(id) +
            '|' +
            RegistryDump_Escape(name) +
            '|' +
            RegistryDump_Escape(mod) +
            '|' +
            RegistryDump_Escape(tags)
        );

        count++;
    }

    RegistryDump_Log(
        'END|BLOCKS|' + count
    );

    return count;
}


// ============================================================
// MACHINES / BLOCK ENTITY TYPES
// ============================================================

function RegistryDump_Machines() {

    RegistryDump_Log('BEGIN|MACHINES');

    var machineRegistry =
        RegistryDump_BuiltInRegistries.BLOCK_ENTITY_TYPE;

    var blockRegistry =
        RegistryDump_BuiltInRegistries.BLOCK;

    var iterator =
        machineRegistry.iterator();

    var count = 0;

    while (iterator.hasNext()) {

        var machineType =
            iterator.next();

        if (machineType == null) {
            continue;
        }

        var machineId =
            RegistryDump_Id(
                machineRegistry,
                machineType
            );

        var machineMod =
            RegistryDump_Mod(machineId);

        var machineBlocks = [];

        try {

            var validBlocks =
                machineType.validBlocks();

            var blockIterator =
                validBlocks.iterator();

            while (blockIterator.hasNext()) {

                var machineBlock =
                    blockIterator.next();

                var machineBlockId =
                    RegistryDump_Id(
                        blockRegistry,
                        machineBlock
                    );

                if (machineBlockId !== 'UNKNOWN') {

                    machineBlocks.push(
                        machineBlockId
                    );
                }
            }

        } catch (e) {

            RegistryDump_Log(
                'MACHINE_BLOCK_ERROR|' +
                RegistryDump_Escape(machineId) +
                '|' +
                RegistryDump_Escape(e)
            );
        }

        RegistryDump_Log(
            'MACHINE|' +
            RegistryDump_Escape(machineId) +
            '|' +
            RegistryDump_Escape(machineMod) +
            '|' +
            RegistryDump_Escape(
                machineBlocks.join(',')
            )
        );

        count++;
    }

    RegistryDump_Log(
        'END|MACHINES|' + count
    );

    return count;
}


// ============================================================
// ENTITIES
// ============================================================

function RegistryDump_Entities() {

    RegistryDump_Log('BEGIN|ENTITIES');

    var registry =
        RegistryDump_BuiltInRegistries.ENTITY_TYPE;

    var iterator =
        registry.iterator();

    var count = 0;

    while (iterator.hasNext()) {

        var entityType =
            iterator.next();

        if (entityType == null) {
            continue;
        }

        var id =
            RegistryDump_Id(
                registry,
                entityType
            );

        var name = '';

        try {
            name =
                entityType
                    .getDescription()
                    .getString();
        } catch (e) {
        }

        var mod =
            RegistryDump_Mod(id);

        RegistryDump_Log(
            'ENTITY|' +
            RegistryDump_Escape(id) +
            '|' +
            RegistryDump_Escape(name) +
            '|' +
            RegistryDump_Escape(mod)
        );

        count++;
    }

    RegistryDump_Log(
        'END|ENTITIES|' + count
    );

    return count;
}


// ============================================================
// RUN
// ============================================================

function RegistryDump_Run() {

    RegistryDump_Log(
        '========================================'
    );

    RegistryDump_Log(
        'REGISTRY DUMP START'
    );

    RegistryDump_Log(
        '========================================'
    );

    var start =
        Date.now();

    var items = 0;
    var blocks = 0;
    var machines = 0;
    var entities = 0;


    try {
        items = RegistryDump_Items();
    } catch (e) {

        RegistryDump_Log(
            'ERROR|ITEMS|' + e
        );
    }


    try {
        blocks = RegistryDump_Blocks();
    } catch (e) {

        RegistryDump_Log(
            'ERROR|BLOCKS|' + e
        );
    }


    try {
        machines = RegistryDump_Machines();
    } catch (e) {

        RegistryDump_Log(
            'ERROR|MACHINES|' + e
        );
    }


    try {
        entities = RegistryDump_Entities();
    } catch (e) {

        RegistryDump_Log(
            'ERROR|ENTITIES|' + e
        );
    }


    var elapsed =
        Date.now() - start;


    RegistryDump_Log(
        'SUMMARY|ITEMS|' + items
    );

    RegistryDump_Log(
        'SUMMARY|BLOCKS|' + blocks
    );

    RegistryDump_Log(
        'SUMMARY|MACHINES|' + machines
    );

    RegistryDump_Log(
        'SUMMARY|ENTITIES|' + entities
    );

    RegistryDump_Log(
        'COMPLETE|' +
        items + ' items|' +
        blocks + ' blocks|' +
        machines + ' machines|' +
        entities + ' entities|' +
        elapsed + 'ms'
    );

    RegistryDump_Log(
        '========================================'
    );

    RegistryDump_Log(
        'REGISTRY DUMP END'
    );

    RegistryDump_Log(
        '========================================'
    );
}


// ============================================================
// COMMAND
// ============================================================

ServerEvents.commandRegistry(function(event) {

    var commands =
        event.commands;

    event.register(
        commands
            .literal('registrydump')
            .requires(function(source) {
                return source.hasPermission(2);
            })
            .executes(function(context) {

                try {

                    RegistryDump_Run();

                    context.source.sendSuccess(
                        function() {
                            return Text.of(
                                'Registry dump complete. Check KubeJS server.log.'
                            );
                        },
                        true
                    );

                    return 1;

                } catch (e) {

                    console.error(
                        '[REGDUMP] FATAL|' + e
                    );

                    return 0;
                }
            })
    );
});
