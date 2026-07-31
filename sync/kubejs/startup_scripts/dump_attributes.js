// kubejs/startup_scripts/dump_attributes.js

const ForgeRegistries = Java.loadClass('net.minecraftforge.registries.ForgeRegistries');
const RangedAttribute = Java.loadClass('net.minecraft.world.entity.ai.attributes.RangedAttribute');

const registry = ForgeRegistries.ATTRIBUTES;

console.info('===== DUMPING REGISTERED ATTRIBUTES =====');

registry.getEntries().forEach(entry => {
    const id = entry.getKey().toString();
    const attr = entry.getValue();
    const className = attr.getClass().getName();

    let details = `ID: ${id}`;
    details += `\nClass: ${className}`;

    if (attr instanceof RangedAttribute) {
        details += `\nDefault value: ${attr.getDefaultValue()}`;
        details += `\nMin value: ${attr.getMinValue()}`;
        details += `\nMax value: ${attr.getMaxValue()}`;
        details += `\nDescription ID: ${attr.getDescriptionId()}`;
    } else {
        details += '\n(Not a RangedAttribute)';
    }

    details += `\nRegistry Name: ${entry.getKey()}`;
    details += '\n---------------------------';

    console.info(details);
});

console.info('===== DUMP COMPLETE =====');
console.info('See full output in logs/kubejs/startup.log');