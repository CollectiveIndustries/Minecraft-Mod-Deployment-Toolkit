ServerEvents.recipes(event => {

  event.replaceInput(
    { output: 'create:rose_quartz' },   // Only recipes producing Rose Quartz
    'minecraft:quartz',                 // Replace Nether Quartz
    'ae2:certus_quartz_crystal'         // With Certus Quartz Crystal
  );

  
  // Controller replacements
  event.replaceInput(
    { output: 'storagedrawers:controller' },
    'minecraft:comparator',
    'ae2:logic_processor'
  )

  event.replaceInput(
    { output: 'storagedrawers:controller' },
    'minecraft:diamond',
    'ae2:engineering_processor'
  )

  // Controller Slave replacements
  event.replaceInput(
    { output: 'storagedrawers:controller_slave' },
    'minecraft:comparator',
    'ae2:logic_processor'
  )

  event.replaceInput(
    { output: 'storagedrawers:controller_slave' },
    'minecraft:gold_ingot',
    'ae2:logic_processor'
  )

    // Replace pistons in all Storage Drawers outputs
  event.replaceInput(
    { mod: 'storagedrawers' },
    'minecraft:piston',
    'create:mechanical_piston'
  )

  // Replace iron ingots in all Storage Drawers outputs
  event.replaceInput(
    { mod: 'storagedrawers' },
    'minecraft:iron_ingot',
    'ae2:calculation_processor'
  )

});