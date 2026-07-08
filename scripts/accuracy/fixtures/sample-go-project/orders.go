// Accuracy fixture for ggoss-treesitter (Go). Orders domain, mirrors the
// py/ts fixtures. Update this file and expected.json together.
package orders

type Order struct {
	ID     int
	Status int
}

func validateOrder(o *Order) bool {
	return o.ID > 0
}

func CreateOrder(id int) bool {
	o := &Order{ID: id}
	return validateOrder(o)
}

func ConfirmOrder(o *Order) bool {
	return validateOrder(o)
}
