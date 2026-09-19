ServerEvents.recipes(event => {

    event.shaped(
        'kubejs:plc_schematic_reader',
        [
            ' B ',
            'SDS',
            'SMS'
        ],
        {
            B: 'create:empty_schematic',
            D: 'create:depot',
            M: 'computercraft:wired_modem',
            S: '#forge:plates/nickel'
        }
    )

    console.info('[PLC] Registered PLC Schematic Reader recipe.')

})