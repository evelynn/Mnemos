namespace SampleProject;

public record Order(string Id, string CustomerId, decimal Total, string Status);

public class OrdersRepo
{
    private readonly List<Order> _rows = new();

    public void Add(Order order)
    {
        _rows.Add(order);
    }

    public Order? GetById(string id)
    {
        return _rows.FirstOrDefault(o => o.Id == id);
    }

    public bool Cancel(string id)
    {
        var o = GetById(id);
        if (o is null) return false;
        // records are immutable — for the fixture we replace in place
        var idx = _rows.IndexOf(o);
        _rows[idx] = o with { Status = "cancelled" };
        return true;
    }
}

public static class OrderService
{
    public static Order CreateOrder(OrdersRepo repo, string id, string customerId, decimal total)
    {
        var order = new Order(id, customerId, total, "pending");
        repo.Add(order);
        return order;
    }

    public static bool ConfirmOrder(OrdersRepo repo, string id)
    {
        var o = repo.GetById(id);
        if (o is null) return false;
        return true;
    }
}
