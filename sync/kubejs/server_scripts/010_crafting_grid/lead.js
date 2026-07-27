ServerEvents.recipes(event => {

  // Remove vanilla lead recipe
  event.remove({ output: 'minecraft:lead' })

  // Add new rod-based recipe
  event.shaped('2x minecraft:lead', [
    'SS ',
    'SR ',
    '  S'
  ], {
    S: 'minecraft:string',
    R: '#forge:rods/iron'
  })

})