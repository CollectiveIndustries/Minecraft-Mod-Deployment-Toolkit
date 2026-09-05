/*
 * ============================================================
 * PLC SCHEMATIC READER
 * BLOCK REGISTRATION
 * ============================================================
 *
 * Minecraft:
 *     1.20.1 Forge
 *
 * KubeJS:
 *     2001.6.5-build.26
 *
 * Create:
 *     6.0.8
 *
 * Purpose:
 *     Register the physical PLC Schematic Reader block.
 *
 * IMPORTANT:
 *     The custom model is generated here using modelJson.
 *
 *     DO NOT replace this with:
 *
 *         ClientEvents.generateAssets(...)
 *
 *     That event does not exist in this KubeJS build.
 * ============================================================
 */


/* ============================================================
 * JAVA CLASSES
 * ============================================================
 */

var JsonParserClass =
    Java.loadClass(
        'com.google.gson.JsonParser'
    )


/* ============================================================
 * CONSTANTS
 * ============================================================
 */

var PLC_ID =
    'kubejs:plc_schematic_reader'


/* ============================================================
 * MODEL JSON
 * ============================================================
 *
 * 13px high depot-derived model.
 *
 * Base body:
 *
 *     0..16 x 0..11 x 0..16
 *
 * Top:
 *
 *     0.95..15.05 x 11..13 x 0.95..15.05
 *
 * Texture paths are the same ones used by the last known
 * working version.
 * ============================================================
 */

function plcBuildModelJson() {

    var jsonText = [

        '{',

        '  "parent": "minecraft:block/block",',

        '  "textures": {',

        '    "body": "immersiveengineering:block/metal/sheetmetal_nickel",',

        '    "top": "create:block/depot_top",',

        '    "particle": "immersiveengineering:block/metal/sheetmetal_nickel"',

        '  },',

        '  "elements": [',

        '    {',

        '      "from": [0, 0, 0],',

        '      "to": [16, 11, 16],',

        '      "faces": {',

        '        "north": {',
        '          "uv": [0, 5, 16, 16],',
        '          "texture": "#body",',
        '          "cullface": "north"',
        '        },',

        '        "east": {',
        '          "uv": [0, 5, 16, 16],',
        '          "texture": "#body",',
        '          "cullface": "east"',
        '        },',

        '        "south": {',
        '          "uv": [0, 5, 16, 16],',
        '          "texture": "#body",',
        '          "cullface": "south"',
        '        },',

        '        "west": {',
        '          "uv": [0, 5, 16, 16],',
        '          "texture": "#body",',
        '          "cullface": "west"',
        '        },',

        '        "up": {',
        '          "uv": [0, 0, 16, 16],',
        '          "texture": "#body",',
        '          "cullface": "up"',
        '        },',

        '        "down": {',
        '          "uv": [0, 0, 16, 16],',
        '          "texture": "#body",',
        '          "cullface": "down"',
        '        }',

        '      }',

        '    },',

        '    {',

        '      "from": [0.95, 11, 0.95],',

        '      "to": [15.05, 13, 15.05],',

        '      "faces": {',

        '        "north": {',
        '          "uv": [1, 14, 15, 16],',
        '          "texture": "#top",',
        '          "cullface": "north"',
        '        },',

        '        "east": {',
        '          "uv": [1, 14, 15, 16],',
        '          "texture": "#top",',
        '          "cullface": "east"',
        '        },',

        '        "south": {',
        '          "uv": [1, 14, 15, 16],',
        '          "texture": "#top",',
        '          "cullface": "south"',
        '        },',

        '        "west": {',
        '          "uv": [1, 14, 15, 16],',
        '          "texture": "#top",',
        '          "cullface": "west"',
        '        },',

        '        "up": {',
        '          "uv": [1, 0, 15, 14],',
        '          "texture": "#top"',

        '        }',

        '      }',

        '    }',

        '  ]',

        '}'

    ].join('\n')


    return JsonParserClass
        .parseString(
            jsonText
        )
        .getAsJsonObject()

}


/* ============================================================
 * BLOCK REGISTRATION
 * ============================================================
 */

StartupEvents.registry(
    'block',

    function(event) {

        var block =
            event.create(
                'plc_schematic_reader'
            )


        /*
         * ------------------------------------------------------
         * BASIC BLOCK INFORMATION
         * ------------------------------------------------------
         */

        block.displayName(
            'PLC Schematic Reader'
        )


        block.hardness(
            2.5
        )


        block.resistance(
            3.5
        )


        block.stoneSoundType()


        /*
         * ------------------------------------------------------
         * DEPOT-LIKE GEOMETRY
         * ------------------------------------------------------
         */

        block.fullBlock(
            false
        )


        block.notSolid()


        block.opaque(
            false
        )


        /*
         * 13px high physical selection/collision shape.
         */

        block.box(
            0,
            0,
            0,
            16,
            13,
            16
        )


        /*
         * ------------------------------------------------------
         * CUSTOM MODEL
         * ------------------------------------------------------
         *
         * The path references the generated model name.
         *
         * modelJson provides the actual geometry.
         */

        block.model(
            'kubejs:block/plc_schematic_reader'
        )


        block.modelJson =
            plcBuildModelJson()


        /*
         * ------------------------------------------------------
         * BLOCK ENTITY
         * ------------------------------------------------------
         *
         * One-slot KubeJS InventoryAttachment.
         *
         * interaction.js is responsible for restricting the
         * physical interaction to Create schematics.
         */

        block.blockEntity(
            function(builder) {

                builder.inventory(
                    1,
                    1
                )

            }
        )

    }
)


/* ============================================================
 * STARTUP REPORT
 * ============================================================
 */

console.info(
    '[PLC] ============================================'
)

console.info(
    '[PLC] PLC SCHEMATIC READER BLOCK'
)

console.info(
    '[PLC] Registered block: ' + PLC_ID
)

console.info(
    '[PLC] Display name: PLC Schematic Reader'
)

console.info(
    '[PLC] Geometry: 13px high'
)

console.info(
    '[PLC] Collision box: 0..16 x 0..13 x 0..16'
)

console.info(
    '[PLC] Full block: false'
)

console.info(
    '[PLC] Solid: false'
)

console.info(
    '[PLC] Opaque: false'
)

console.info(
    '[PLC] Model: kubejs:block/plc_schematic_reader'
)

console.info(
    '[PLC] Model source: block.modelJson'
)

console.info(
    '[PLC] BlockEntity: KubeJS BlockEntityJS'
)

console.info(
    '[PLC] Inventory: KubeJS InventoryAttachment'
)

console.info(
    '[PLC] Inventory slots: 1'
)

console.info(
    '[PLC] Item filtering: interaction.js'
)

console.info(
    '[PLC] ============================================'
)
