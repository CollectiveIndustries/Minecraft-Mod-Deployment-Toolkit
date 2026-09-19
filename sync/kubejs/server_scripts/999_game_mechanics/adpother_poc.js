/*
 * AdPother physical gas collector proof of concept.
 *
 * /adpother_probe
 *     Find the nearest Carbon/Sulfur GasEntity within 8 blocks
 *     and report its current pollution amount and capacity.
 *
 * /adpother_collect
 *     Find the nearest Carbon/Sulfur GasEntity within 8 blocks,
 *     call AdPother's own spend() method, and report the result.
 *
 * /adpother_all
 *     Report every Carbon/Sulfur GasEntity within 64 blocks,
 *     including world position, offset from the player, and distance.
 *
 * This POC does not:
 *     - create fluids
 *     - modify ChunkPollution
 *     - add a Create machine
 *
 * It exists only to verify the physical GasEntity extraction path.
 */

const GasEntity =
    Java.loadClass(
        'com.endertech.minecraft.mods.adpother.entities.GasEntity'
    );

const Carbon =
    Java.loadClass(
        'com.endertech.minecraft.mods.adpother.blocks.Carbon'
    );

const Sulfur =
    Java.loadClass(
        'com.endertech.minecraft.mods.adpother.blocks.Sulfur'
    );

const Component =
    Java.loadClass(
        'net.minecraft.network.chat.Component'
    );


function tell(player, message) {
    player.tell(
        Component.literal(message)
    );
}


function getPollutantType(entity) {
    const pollutant =
        entity.getPollutant().orElse(null);

    if (pollutant === null) {
        return null;
    }

    if (pollutant instanceof Carbon) {
        return 'carbon';
    }

    if (pollutant instanceof Sulfur) {
        return 'sulfur';
    }

    return null;
}


function findGas(player) {
    const entities =
        player.level.getEntitiesOfClass(
            GasEntity,
            player.getBoundingBox().inflate(8)
        );

    let nearest = null;
    let nearestDistance = Number.MAX_VALUE;

    for (const entity of entities) {
        const type =
            getPollutantType(entity);

        if (type === null) {
            continue;
        }

        const distance =
            entity.distanceToSqr(player);

        if (distance < nearestDistance) {
            nearest = entity;
            nearestDistance = distance;
        }
    }

    return nearest;
}


function inspect(player) {
    const entity =
        findGas(player);

    if (entity === null) {
        tell(
            player,
            '[AdPother POC] No Carbon or Sulfur GasEntity found within 8 blocks.'
        );
        return;
    }

    const type =
        getPollutantType(entity);

    const amount =
        entity.getPollutionAmount();

    const capacity =
        entity.getPollutionCapacity();

    const id =
        entity.getId();

    tell(
        player,
        '[AdPother POC] ' +
        'type=' + type +
        ' amount=' + amount +
        ' capacity=' + capacity +
        ' id=' + id
    );
}


function collect(player) {
    const entity =
        findGas(player);

    if (entity === null) {
        tell(
            player,
            '[AdPother POC] No Carbon or Sulfur GasEntity found within 8 blocks.'
        );
        return;
    }

    const typeBefore =
        getPollutantType(entity);

    const amountBefore =
        entity.getPollutionAmount();

    const capacity =
        entity.getPollutionCapacity();

    const id =
        entity.getId();

    const spent =
        entity.spend();

    if (!spent) {
        tell(
            player,
            '[AdPother POC] spend() FAILED ' +
            'type=' + typeBefore +
            ' amount=' + amountBefore +
            ' capacity=' + capacity +
            ' id=' + id
        );
        return;
    }

    const aliveAfter =
        entity.isAlive();

    const amountAfter =
        entity.getPollutionAmount();

    const typeAfter =
        getPollutantType(entity);

    tell(
        player,
        '[AdPother POC] spend() OK ' +
        'id=' + id +
        ' ' + typeBefore +
        ' ' + amountBefore +
        ' -> ' +
        typeAfter +
        ' ' + amountAfter +
        ' alive=' + aliveAfter
    );
}


function inspectAll(player) {
    const radius = 64;

    const entities =
        player.level.getEntitiesOfClass(
            GasEntity,
            player.getBoundingBox().inflate(radius)
        );

    const px = player.getX();
    const py = player.getY();
    const pz = player.getZ();

    tell(
        player,
        '[AdPother POC] === carriers within ' + radius + ' blocks ==='
    );

    tell(
        player,
        '[AdPother POC] player pos=' +
        px.toFixed(2) + ',' +
        py.toFixed(2) + ',' +
        pz.toFixed(2)
    );

    let count = 0;

    for (const entity of entities) {
        const type =
            getPollutantType(entity);

        if (type === null) {
            continue;
        }

        const x = entity.getX();
        const y = entity.getY();
        const z = entity.getZ();

        const dx = x - px;
        const dy = y - py;
        const dz = z - pz;

        const dist =
            Math.sqrt(dx * dx + dy * dy + dz * dz);

        tell(
            player,
            '[AdPother POC] id=' + entity.getId() +
            ' type=' + type +
            ' amount=' + entity.getPollutionAmount() +
            ' capacity=' + entity.getPollutionCapacity() +
            ' pos=' + x.toFixed(2) + ',' + y.toFixed(2) + ',' + z.toFixed(2) +
            ' offset=' + dx.toFixed(2) + ',' + dy.toFixed(2) + ',' + dz.toFixed(2) +
            ' dist=' + dist.toFixed(2)
        );

        count++;
    }

    tell(
        player,
        '[AdPother POC] carriers=' + count
    );
}


ServerEvents.commandRegistry(event => {
    const Commands =
        event.commands;

    event.register(
        Commands.literal('adpother_probe')
            .requires(source => source.getPlayer() !== null)
            .executes(context => {
                inspect(
                    context.source.getPlayer()
                );

                return 1;
            })
    );

    event.register(
        Commands.literal('adpother_collect')
            .requires(source => source.getPlayer() !== null)
            .executes(context => {
                collect(
                    context.source.getPlayer()
                );

                return 1;
            })
    );

    event.register(
        Commands.literal('adpother_all')
            .requires(source => source.getPlayer() !== null)
            .executes(context => {
                inspectAll(
                    context.source.getPlayer()
                );

                return 1;
            })
    );
});


console.info(
    '=== AdPother physical gas collector POC loaded ==='
);
