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
 * Prevent Create's schematic placement handler from consuming
 * a right-click on the PLC Schematic Reader.
 *
 * We intercept the mouse event at HIGHEST priority and forward
 * it through Minecraft's normal useItemOn() pathway.
 *
 * The server then receives:
 *
 *     PlayerInteractEvent.RightClickBlock
 *
 * and performs the actual inventory operation.
 *
 * IMPORTANT
 * ---------
 *
 * ForgeEvents is NOT available in this client_scripts
 * environment.
 *
 * We therefore register directly against MinecraftForge.EVENT_BUS
 * using an explicit java.util.function.Consumer.
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
 * ITEM TEST
 * ============================================================
 */

function plcIsEmpty(stack) {

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


function plcItemId(stack) {

    try {

        if (
            plcIsEmpty(
                stack
            )
        ) {

            return null

        }


        return String(
            stack.id
        )

    } catch (error) {

        try {

            return String(
                stack.getItem()
            )

        } catch (ignored) {

            return null

        }

    }

}


function plcIsSchematic(stack) {

    return (
        plcItemId(
            stack
        )
        ===
        SCHEMATIC_ID
    )

}


/* ============================================================
 * BLOCK TEST
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
         * Fallback.
         */

        try {

            return (
                String(
                    state.getBlock()
                )
                ===
                'Block{' + PLC_ID + '}'
            )

        } catch (ignored) {

            return false

        }

    } catch (error) {

        return false

    }

}


/* ============================================================
 * MOUSE HANDLER
 * ============================================================
 */

function plcHandleMouse(
    event
) {

    try {

        /*
         * ------------------------------------------------------
         * RIGHT MOUSE BUTTON
         * ------------------------------------------------------
         *
         * GLFW:
         *
         *     1 = right button
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


        /*
         * ------------------------------------------------------
         * PRESS ONLY
         * ------------------------------------------------------
         *
         * GLFW:
         *
         *     1 = PRESS
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


        /*
         * ------------------------------------------------------
         * MINECRAFT CLIENT
         * ------------------------------------------------------
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


        /*
         * ------------------------------------------------------
         * NO GUI
         * ------------------------------------------------------
         */

        if (
            mc.screen
            !==
            null
        ) {

            return

        }


        /*
         * ------------------------------------------------------
         * ONLY WHILE HOLDING A SCHEMATIC
         * ------------------------------------------------------
         */

        var held =
            mc.player.getMainHandItem()


        if (
            !plcIsSchematic(
                held
            )
        ) {

            /*
             * Empty-hand extraction is intentionally allowed to
             * use the normal block interaction pathway.
             *
             * Create does not need to steal an empty-hand click,
             * so we only intercept schematic-in-hand clicks.
             */

            return

        }


        /*
         * ------------------------------------------------------
         * MUST BE A BLOCK HIT
         * ------------------------------------------------------
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


        var pos =
            hit.getBlockPos()


        if (!pos) {

            return

        }


        /*
         * ------------------------------------------------------
         * MUST BE OUR PLC READER
         * ------------------------------------------------------
         */

        if (
            !plcIsReaderBlock(
                mc.level,
                pos
            )
        ) {

            return

        }


        /*
         * ------------------------------------------------------
         * TOP FACE ONLY
         * ------------------------------------------------------
         */

        if (
            hit.getDirection()
            !==
            DirectionClass.UP
        ) {

            return

        }


        console.info(
            '[PLC] >>> PLC SCHEMATIC TARGET CONFIRMED <<<'
        )


        /*
         * ------------------------------------------------------
         * FORWARD TO SERVER
         * ------------------------------------------------------
         *
         * This triggers the normal Forge
         * PlayerInteractEvent.RightClickBlock event.
         */

        console.info(
            '[PLC] Calling gameMode.useItemOn().'
        )


        mc.gameMode.useItemOn(
            mc.player,
            InteractionHandClass.MAIN_HAND,
            hit
        )


        /*
         * ------------------------------------------------------
         * CANCEL CREATE'S ORIGINAL MOUSE EVENT
         * ------------------------------------------------------
         */

        event.setCanceled(
            true
        )


        console.info(
            '[PLC] Original mouse event canceled.'
        )

    } catch (error) {

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

        accept: function(event) {

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
    '[PLC] Right mouse: enabled'
)

console.info(
    '[PLC] Top face: required'
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
