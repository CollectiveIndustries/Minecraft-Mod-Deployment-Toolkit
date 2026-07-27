// kubejs/server_scripts/020_compatibility/minecraft_storagedrawers.js
ServerEvents.recipes(event => {

  // Copper Hopper: 2 Copper Ingots + any Full Drawer → 2 Hoppers
  event.shaped(
    Item.of('minecraft:hopper', 2),               // output
    [
      'C C',
      'CBC',
      ' C '
    ],
    {
      C: 'minecraft:copper_ingot',
      B: '#storagedrawers:full_drawers'          // all full drawers (any wood/size)
    }
  ).id('kubejs:copper_hopper');

});