/*
 * ============================================================
 * PLC SCHEMATIC READER
 * CLIENT MOUSE INTERCEPTOR
 * ============================================================
 *
 * Minecraft:
 *     1.20.1 Forge
 *
 * KubeJS:
 *     2001.6.5-build.26
 *
 * PURPOSE
 * -------
 *
 * Intercept ONLY:
 *
 *     RMB
 *     +
 *     mouse PRESS
 *     +
 *     no GUI
 *     +
 *     holding create:schematic
 *     +
 *     target is PLC Schematic Reader
 *     +
 *     target face is TOP
 *
 * Then forward the interaction through:
 *
 *     Minecraft.gameMode.useItemOn()
 *
 * so the server-side PLC interaction handler receives the
 * normal PlayerInteractEvent.RightClickBlock event.
 *
 * EVERYTHING ELSE IS LEFT COMPLETELY ALONE.
 *
 * In particular:
 *
 *     - hoes
 *     - blocks
 *     - tools
 *     - Create machines
 *     - CC modems
 *     - empty hand
 *     - containers
 *     - normal Create interactions
 *
 * are NOT intercepted here.
 *
 * ============================================================
 */


/* ============================================================
 * JAVA CLASSES
 * ============================================================
 */

var MinecraftClass =
    Java.loadClass(
        'net.minecraft.client.Minecraft'
    )

var DirectionClass =
    Java.loadClass(
        'net.minecraft.core.Direction'
    )

var InteractionHandClass =
    Java.loadClass(
        'net.minecraft.world.InteractionHand'
    )

var BlockHitResultClass =
    Java.loadClass(
        'net.minecraft.world.phys.BlockHitResult'
    )

var MinecraftForgeClass =
    Java.loadClass(
        'net.minecraftforge.common.MinecraftForge'
    )

var EventPriorityClass =
    Java.loadClass(
        'net.minecraftforge.eventbus.api.EventPriority'
    )

var MouseButtonPreClass =
    Java.loadClass(
        'net.minecraftforge.client.event.InputEvent$MouseButton$Pre'
    )

var ConsumerClass =
    Java.loadClass(
        'java.util.function.Consumer'
    )


/* ============================================================
 * CONSTANTS
 * ============================================================
 */

var PLC_ID =
    'kubejs:plc_schematic_reader'

var SCHEMATIC_ID =
    'create:schematic'


/* ============================================================
 * SAFE HELPERS
 * ============================================================
 */

function plcIsEmpty(
    stack
) {

    try {

        return (
            stack === null
            ||
            stack === undefined
            ||
            stack.isEmpty()
        )

    } catch (error) {

        return true

    }

}


/*
 * Resolve the item registry ID.
 *
 * KubeJS/Rhino commonly exposes:
 *
 *     stack.id
 *
 * We retain the Java fallback for compatibility.
 */

function plcItemId(
    stack
) {

    if (
        plcIsEmpty(
            stack
        )
    ) {

        return null

    }


    try {

        var id =
            stack.id

        if (
            id !== null
            &&
            id !== undefined
        ) {

            return String(
                id
            )

        }

    } catch (error) {

    }


    /*
     * Java fallback.
     */

    try {

        var item =
            stack.getItem()

        if (
            item !== null
            &&
            item !== undefined
        ) {

            /*
             * KubeJS normally gives us the registry ID through
             * stack.id, so this fallback is deliberately only
             * used when that path is unavailable.
             */

            try {

                if (
                    item.id !== null
                    &&
                    item.id !== undefined
                ) {

                    return String(
                        item.id
                    )

                }

            } catch (ignored) {

            }

        }

    } catch (error) {

    }


    return null

}


/*
 * Exact Create schematic test.
 *
 * IMPORTANT:
 *
 * This is the FIRST item-specific gate.
 *
 * A modem, hoe, block, wrench, empty hand, etc. therefore
 * never reaches the PLC target logic.
 */

function plcIsSchematic(
    stack
) {

    return (
        plcItemId(
            stack
        )
        ===
        SCHEMATIC_ID
    )

}


/* ============================================================
 * PLC BLOCK IDENTIFICATION
 * ============================================================
 */

function plcIsReaderBlock(
    level,
    pos
) {

    try {

        if (
            !level
            ||
            !pos
        ) {

            return false

        }


        var state =
            level.getBlockState(
                pos
            )


        if (!state) {

            return false

        }


        /*
         * Preferred KubeJS registry ID.
         */

        try {

            if (
                String(
                    state.getBlock().id
                )
                ===
                PLC_ID
            ) {

                return true

            }

        } catch (ignored) {

        }


        /*
         * Fallback registry string.
         */

        try {

            if (
                String(
                    state.getBlock()
                )
                ===
                'Block{' + PLC_ID + '}'
            ) {

                return true

            }

        } catch (ignored) {

        }


        return false

    } catch (error) {

        return false

    }

}


/* ============================================================
 * MAIN HANDLER
 * ============================================================
 */

function plcHandleMouse(
    event
) {

    /*
     * Everything below this point is protected by increasingly
     * specific filters.
     *
     * If ANY filter fails, we simply return.
     *
     * We DO NOT cancel the mouse event.
     */


    try {

        /* ----------------------------------------------------
         * BUTTON
         * ----------------------------------------------------
         *
         * GLFW:
         *
         *     1 = right mouse button
         */

        if (
            Number(
                event.getButton()
            )
            !==
            1
        ) {

            return

        }


        /* ----------------------------------------------------
         * ACTION
         * ----------------------------------------------------
         *
         * GLFW:
         *
         *     1 = PRESS
         *
         * Release events are ignored.
         */

        if (
            Number(
                event.getAction()
            )
            !==
            1
        ) {

            return

        }


        /* ----------------------------------------------------
         * CLIENT
         * ----------------------------------------------------
         */

        var mc =
            MinecraftClass.getInstance()


        if (
            !mc
            ||
            !mc.player
            ||
            !mc.level
            ||
            !mc.gameMode
            ||
            !mc.hitResult
        ) {

            return

        }


        /* ----------------------------------------------------
         * GUI
         * ----------------------------------------------------
         *
         * Never intercept clicks while a GUI is open.
         */

        if (
            mc.screen
            !==
            null
        ) {

            return

        }


        /* ----------------------------------------------------
         * HELD ITEM
         * ----------------------------------------------------
         *
         * THIS IS THE CRITICAL FILTER.
         *
         * Nothing except create:schematic is allowed to pass.
         */

        var held =
            mc.player.getMainHandItem()


        if (
            !plcIsSchematic(
                held
            )
        ) {

            return

        }


        /* ----------------------------------------------------
         * HIT TYPE
         * ----------------------------------------------------
         */

        if (
            !(
                mc.hitResult
                instanceof
                BlockHitResultClass
            )
        ) {

            return

        }


        var hit =
            mc.hitResult


        /* ----------------------------------------------------
         * TARGET POSITION
         * ----------------------------------------------------
         */

        var pos =
            hit.getBlockPos()


        if (!pos) {

            return

        }


        /* ----------------------------------------------------
         * TARGET BLOCK
         * ----------------------------------------------------
         */

        if (
            !plcIsReaderBlock(
                mc.level,
                pos
            )
        ) {

            return

        }


        /* ----------------------------------------------------
         * TARGET FACE
         * ----------------------------------------------------
         *
         * PLC accepts schematics from TOP only.
         */

        if (
            hit.getDirection()
            !==
            DirectionClass.UP
        ) {

            return

        }


        /* ====================================================
         * EVERYTHING ABOVE THIS POINT WAS ONLY FILTERING.
         *
         * We now KNOW this is:
         *
         *     RMB PRESS
         *     + schematic
         *     + PLC reader
         *     + TOP
         *
         * This is the ONLY situation where we take control.
         * ====================================================
         */

        console.info(
            '[PLC] >>> PLC SCHEMATIC TARGET CONFIRMED <<<'
        )


        /* ----------------------------------------------------
         * FORWARD NORMAL INTERACTION
         * ----------------------------------------------------
         *
         * This sends the interaction to the server where
         * interaction.js performs:
         *
         *     insert
         *     swap
         *
         * and then returns SUCCESS.
         */

        try {

            mc.gameMode.useItemOn(
                mc.player,
                InteractionHandClass.MAIN_HAND,
                hit
            )

        } catch (error) {

            /*
             * If forwarding fails, log it but DO NOT attempt
             * additional interaction behavior.
             */

            console.error(
                '[PLC] gameMode.useItemOn() failed: '
                +
                String(error)
            )

            return

        }


        /* ----------------------------------------------------
         * CANCEL ORIGINAL MOUSE EVENT
         * ----------------------------------------------------
         *
         * THIS is intentionally the final operation.
         *
         * Nothing else gets canceled.
         */

        try {

            event.setCanceled(
                true
            )

        } catch (error) {

            console.error(
                '[PLC] Failed to cancel original mouse event: '
                +
                String(error)
            )

            return

        }


        console.info(
            '[PLC] Original schematic mouse event canceled.'
        )

    } catch (error) {

        /*
         * Defensive top-level boundary.
         *
         * We never allow an exception in the interceptor to
         * escape into the Forge EventBus.
         */

        console.error(
            '[PLC] Client schematic interceptor error: '
            +
            String(error)
        )

    }

}


/* ============================================================
 * EXPLICIT JAVA CONSUMER
 * ============================================================
 */

var plcMouseConsumer =
    new ConsumerClass({

        accept:
            function(
                event
            ) {

                plcHandleMouse(
                    event
                )

            }

    })


/* ============================================================
 * EVENT REGISTRATION
 * ============================================================
 */

try {

    console.info(
        '[PLC] Registering HIGHEST schematic interceptor.'
    )


    MinecraftForgeClass.EVENT_BUS.addListener(
        EventPriorityClass.HIGHEST,
        false,
        MouseButtonPreClass,
        plcMouseConsumer
    )


    console.info(
        '[PLC] HIGHEST schematic interceptor registered.'
    )

} catch (error) {

    console.error(
        '[PLC] Failed to register client interceptor: '
        +
        String(error)
    )

}


/* ============================================================
 * STARTUP REPORT
 * ============================================================
 */

console.info(
    '[PLC] ============================================'
)

console.info(
    '[PLC] PLC SCHEMATIC READER CLIENT INTERCEPTOR'
)

console.info(
    '[PLC] Block: ' + PLC_ID
)

console.info(
    '[PLC] Item: ' + SCHEMATIC_ID
)

console.info(
    '[PLC] Event: InputEvent.MouseButton.Pre'
)

console.info(
    '[PLC] Priority: HIGHEST'
)

console.info(
    '[PLC] Right mouse: filtered'
)

console.info(
    '[PLC] Held item: create:schematic ONLY'
)

console.info(
    '[PLC] Target block: PLC Reader ONLY'
)

console.info(
    '[PLC] Target face: TOP ONLY'
)

console.info(
    '[PLC] GUI interaction: ignored'
)

console.info(
    '[PLC] Empty hand: ignored'
)

console.info(
    '[PLC] Non-schematic items: ignored'
)

console.info(
    '[PLC] Modems: ignored'
)

console.info(
    '[PLC] Ordinary Minecraft interactions: ignored'
)

console.info(
    '[PLC] Create schematic placement interception: enabled'
)

console.info(
    '[PLC] Server interaction forwarded through useItemOn()'
)

console.info(
    '[PLC] ============================================'
)
